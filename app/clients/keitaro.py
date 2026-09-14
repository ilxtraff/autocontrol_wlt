"""Клиент Keitaro Admin API.

Статистика для автоконтроля берётся отсюда, а не из Facebook. Отчёт строится
одним запросом на группу адсетов: /admin_api/v1/report/build с группировкой по
полю, в котором лежит ID адсета: это тот sub_id, на который в настройках
трекера замаплен параметр adset_id={{adset.id}} из ссылки.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.rules import Metrics
from app.tzwindow import DayWindow, to_tracker_range

log = logging.getLogger(__name__)

# Названия метрик в отчёте меняются от версии к версии — пробуем по очереди.
_SPEND_KEYS = ("cost", "campaign_unique_cost", "sale_revenue_cost", "expenses")
_CLICK_KEYS = ("clicks", "campaign_clicks")
_UNIQUE_CLICK_KEYS = ("campaign_unique_clicks", "unique_clicks", "global_unique_clicks")
_CONVERSION_KEYS = ("conversions", "campaign_conversions")
_LEAD_KEYS = ("leads", "lead_conversions")
_SALE_KEYS = ("sales", "sale_conversions")
_REVENUE_KEYS = ("revenue", "sale_revenue")

REPORT_METRICS = [
    "clicks",
    "campaign_unique_clicks",
    "conversions",
    "leads",
    "sales",
    "revenue",
    "cost",
]


class KeitaroError(RuntimeError):
    """Трекер не ответил или ответил ошибкой."""


def _num(row: dict, keys: tuple[str, ...]) -> float:
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return 0.0


def _row_to_metrics(row: dict) -> Metrics:
    return Metrics(
        spend=_num(row, _SPEND_KEYS),
        conversions=int(_num(row, _CONVERSION_KEYS)),
        leads=int(_num(row, _LEAD_KEYS)),
        sales=int(_num(row, _SALE_KEYS)),
        clicks=int(_num(row, _CLICK_KEYS)),
        unique_clicks=int(_num(row, _UNIQUE_CLICK_KEYS)),
        revenue=_num(row, _REVENUE_KEYS),
    )


@dataclass
class KeitaroClient:
    base_url: str
    api_key: str
    timezone_name: str = "Europe/Moscow"
    adset_field: str = "sub_id_6"
    timeout: int = 45

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def fetch_adset_metrics(
        self,
        window: DayWindow,
        adset_ids: list[str],
        client: httpx.Client | None = None,
    ) -> dict[str, Metrics]:
        """Метрики по каждому адсету за окно рекламного дня.

        Окно приходит в абсолютном времени (00:00 по кабинету) и здесь
        пересчитывается в локальное время трекера — Keitaro живёт по Москве.
        """
        if not adset_ids:
            return {}

        date_from, date_to = to_tracker_range(window, self.timezone_name)
        payload = {
            "range": {
                "from": date_from,
                "to": date_to,
                "timezone": self.timezone_name,
            },
            "grouping": [self.adset_field],
            "metrics": REPORT_METRICS,
            "filters": [
                {
                    "name": self.adset_field,
                    "operator": "IN_LIST",
                    "expression": list(adset_ids),
                }
            ],
            "limit": max(len(adset_ids) * 2, 1000),
            "offset": 0,
        }

        owns_client = client is None
        client = client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                self._url("/admin_api/v1/report/build"),
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise KeitaroError(f"Keitaro недоступна: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        if response.status_code >= 400:
            raise KeitaroError(
                f"Keitaro вернула {response.status_code}: {response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise KeitaroError("Keitaro вернула не-JSON") from exc

        rows = body.get("rows") if isinstance(body, dict) else None
        if rows is None:
            rows = body if isinstance(body, list) else []

        result: dict[str, Metrics] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get(self.adset_field)
            if key in (None, ""):
                continue
            result[str(key)] = _row_to_metrics(row)

        # Адсеты без трафика Keitaro не возвращает — это нули, а не отсутствие данных.
        for adset_id in adset_ids:
            result.setdefault(str(adset_id), Metrics())
        return result

    def probe_adset_field(
        self,
        window: DayWindow,
        sample_ids: list[str],
        client: httpx.Client | None = None,
    ) -> list[str]:
        """Ищет, в каком sub_id трекера реально лежат ID адсетов.

        Номер зависит от того, на какой sub_id в настройках Keitaro замаплен
        параметр из ссылки, — у всех по-разному. Перебираем sub_id_1..15 и
        возвращаем те, где нашлись знакомые ID.
        """
        if not sample_ids:
            return []

        date_from, date_to = to_tracker_range(window, self.timezone_name)
        owns_client = client is None
        client = client or httpx.Client(timeout=self.timeout)
        found: list[str] = []
        answered = False
        last_error = ""
        try:
            for index in range(1, 16):
                field = f"sub_id_{index}"
                payload = {
                    "range": {"from": date_from, "to": date_to, "timezone": self.timezone_name},
                    "grouping": [field],
                    "metrics": ["clicks"],
                    "filters": [
                        {"name": field, "operator": "IN_LIST", "expression": list(sample_ids)}
                    ],
                    "limit": 10,
                }
                try:
                    response = client.post(
                        self._url("/admin_api/v1/report/build"),
                        json=payload, headers=self._headers(), timeout=self.timeout,
                    )
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    continue
                if response.status_code >= 400:
                    last_error = f"{response.status_code}: {response.text[:200]}"
                    continue
                try:
                    body = response.json()
                except ValueError:
                    last_error = "ответ не JSON"
                    continue
                answered = True
                rows = body.get("rows") if isinstance(body, dict) else None
                if not rows:
                    continue
                # Строка засчитывается, только если в ней действительно наш ID.
                wanted = {str(i) for i in sample_ids}
                if any(str(row.get(field)) in wanted for row in rows if isinstance(row, dict)):
                    found.append(field)
        finally:
            if owns_client:
                client.close()

        if not answered:
            # Ни один запрос не прошёл — это недоступный трекер, а не «не нашли».
            raise KeitaroError(f"Keitaro не ответила ни на один запрос: {last_error}")
        return found

    def ping(self) -> bool:
        """Быстрая проверка ключа."""
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(self._url("/admin_api/v1/campaigns"), headers=self._headers())
        except httpx.HTTPError as exc:
            raise KeitaroError(f"Keitaro недоступна: {exc}") from exc
        if response.status_code >= 400:
            raise KeitaroError(f"Keitaro вернула {response.status_code}")
        return True
