"""Клиент Facebook Marketing API.

Нужен ровно для трёх вещей: узнать таймзону кабинета, выключить адсет и
включить его обратно. Токен берётся из социального аккаунта.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

STATUS_ACTIVE = "ACTIVE"
STATUS_PAUSED = "PAUSED"


class FacebookError(RuntimeError):
    """Graph API вернул ошибку."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    @property
    def is_token_problem(self) -> bool:
        # 190 — протухший/отозванный токен, 102 — сессия невалидна.
        return self.code in {190, 102}


@dataclass
class FacebookClient:
    access_token: str
    api_version: str = "v21.0"
    timeout: int = 45

    def _url(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        params = kwargs.pop("params", {}) or {}
        params.setdefault("access_token", self.access_token)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(method, self._url(path), params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise FacebookError(f"Facebook недоступен: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code >= 400 or "error" in body:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise FacebookError(
                error.get("message") or f"Facebook вернул {response.status_code}",
                code=error.get("code"),
                subcode=error.get("error_subcode"),
            )
        return body if isinstance(body, dict) else {}

    # ---------------------------------------------------------------- кабинет

    def get_account(self, account_id: str) -> dict:
        """Данные кабинета, включая timezone_name — по нему строится день."""
        return self._request(
            "GET",
            _act(account_id),
            params={"fields": "id,name,account_status,timezone_name,timezone_offset_hours_utc,currency"},
        )

    def list_accounts(self) -> list[dict]:
        """Кабинеты, доступные токену социального аккаунта."""
        body = self._request(
            "GET",
            "me/adaccounts",
            params={
                "fields": "id,account_id,name,account_status,timezone_name,currency",
                "limit": 200,
            },
        )
        return body.get("data", [])

    # ----------------------------------------------------------------- адсеты

    def get_adset(self, adset_id: str) -> dict:
        return self._request(
            "GET",
            adset_id,
            params={"fields": "id,name,status,effective_status,campaign_id,account_id"},
        )

    def get_adsets_status(self, adset_ids: list[str]) -> dict[str, dict]:
        """Статусы пачкой — чтобы не дёргать Graph на каждый адсет."""
        if not adset_ids:
            return {}
        body = self._request(
            "GET",
            "",
            params={"ids": ",".join(adset_ids), "fields": "id,name,status,effective_status"},
        )
        return {key: value for key, value in body.items() if isinstance(value, dict)}

    def list_adsets(self, account_id: str, statuses: list[str] | None = None) -> list[dict]:
        """Все адсеты кабинета с их кампаниями, страницами по 200."""
        params = {
            "fields": "id,name,status,effective_status,campaign{id,name}",
            "limit": 200,
        }
        if statuses:
            import json as _json

            params["filtering"] = _json.dumps(
                [{"field": "effective_status", "operator": "IN", "value": statuses}]
            )

        out: list[dict] = []
        path = _act(account_id) + "/adsets"
        while True:
            body = self._request("GET", path, params=params)
            out.extend(body.get("data", []))
            after = (body.get("paging", {}).get("cursors", {}) or {}).get("after")
            if not after or not body.get("paging", {}).get("next"):
                break
            params = dict(params, after=after)
        return out

    def set_adset_status(self, adset_id: str, status: str) -> bool:
        self._request("POST", adset_id, params={"status": status})
        return True

    def pause_adset(self, adset_id: str) -> bool:
        return self.set_adset_status(adset_id, STATUS_PAUSED)

    def resume_adset(self, adset_id: str) -> bool:
        return self.set_adset_status(adset_id, STATUS_ACTIVE)

    def check_token(self) -> dict:
        return self._request("GET", "me", params={"fields": "id,name"})


def _act(account_id: str) -> str:
    account_id = str(account_id).strip()
    return account_id if account_id.startswith("act_") else f"act_{account_id}"
