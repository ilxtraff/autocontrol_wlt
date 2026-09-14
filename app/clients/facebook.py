"""Клиент Facebook Marketing API.

Нужен ровно для трёх вещей: узнать таймзону кабинета, выключить адсет и
включить его обратно. Работает от имени социального аккаунта целиком:

  * запросы идут через **прокси аккаунта** — с чужого IP Facebook быстро
    выдаёт чекпоинт, и никакой автоконтроль это не переживёт;
  * с тем же **user-agent**, что у браузера аккаунта;
  * **куки** самим вызовам Graph API не нужны, там хватает токена, но по ним
    токен перевыпускается, когда протухает.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

STATUS_ACTIVE = "ACTIVE"
STATUS_PAUSED = "PAUSED"


class FacebookError(RuntimeError):
    """Graph API вернул ошибку."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        kind: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.kind = kind

    def __str__(self) -> str:
        # Без кода «Invalid request.» ничего не говорит о причине.
        base = super().__str__()
        bits = [f"#{self.code}"] if self.code is not None else []
        if self.subcode:
            bits.append(f"подкод {self.subcode}")
        return f"{base} ({', '.join(bits)})" if bits else base

    @property
    def is_transport_problem(self) -> bool:
        """Сеть, а не Facebook: менять токен бессмысленно."""
        return self.kind == "transport"

    @property
    def is_token_problem(self) -> bool:
        # 190 — протухший или отозванный токен, 102 — сессия невалидна.
        return self.code in {190, 102}

    @property
    def is_permission_problem(self) -> bool:
        # 200 и 10 — прав не хватает, 3 — метод недоступен приложению.
        return self.code in {200, 10, 3}

    @property
    def hint(self) -> str:
        """Человеческая причина — то, что стоит показать в консоли."""
        if self.is_transport_problem:
            return "до graph.facebook.com не достучаться — проверьте сеть сервера"
        if self.is_token_problem:
            return "токен протух или отозван — перевыпустите его"
        if self.is_permission_problem:
            return "токену не хватает прав ads_management / ads_read"
        if self.code == 100:
            return (
                "Facebook не понял запрос. Чаще всего это токен системного "
                "пользователя: у него нет пути me/adaccounts"
            )
        if self.code == 17 or self.code == 613:
            return "упёрлись в лимит запросов Facebook — подождите и повторите"
        return ""


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Страницы, на которых у залогиненной сессии обычно лежит рабочий токен.
_TOKEN_SOURCES = (
    "https://business.facebook.com/business_locations",
    "https://business.facebook.com/content_management",
    "https://www.facebook.com/adsmanager/manage/campaigns",
)

# Токен на этих страницах встречается в разной обёртке.
_TOKEN_PATTERNS = (
    re.compile(r'"accessToken"\s*:\s*"(EAA[A-Za-z0-9_\-]{20,})"'),
    re.compile(r'access_token=(EAA[A-Za-z0-9_\-]{20,})'),
    re.compile(r'\bEAA[A-Za-z0-9_\-]{50,}\b'),
)


@dataclass
class FacebookClient:
    access_token: str
    api_version: str = "v21.0"
    timeout: int = 45
    proxy: str = ""
    cookies: str = ""
    user_agent: str = ""
    _transport: object = field(default=None, repr=False, compare=False)

    def _url(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{path.lstrip('/')}"

    def _safe_user_agent(self) -> str:
        """Заголовки кодируются в latin-1: не-ASCII user-agent уронил бы запрос.

        Такое прилетает из кривых выгрузок мультитокена, и падать из-за этого
        всем аккаунтом нельзя — берём дефолтный.
        """
        agent = (self.user_agent or "").strip()
        if not agent:
            return DEFAULT_USER_AGENT
        try:
            agent.encode("latin-1")
        except UnicodeEncodeError:
            log.warning("user-agent содержит не-ASCII символы, беру стандартный")
            return DEFAULT_USER_AGENT
        return agent

    def _client_kwargs(self) -> dict:
        kwargs: dict = {
            "timeout": self.timeout,
            "headers": {"User-Agent": self._safe_user_agent()},
            "follow_redirects": True,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self._transport is not None:
            # Подменённый транспорт в тестах; с ним прокси не нужен.
            kwargs["transport"] = self._transport
            kwargs.pop("proxy", None)
        return kwargs

    def _cookie_jar(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        for chunk in (self.cookies or "").split(";"):
            chunk = chunk.strip()
            if "=" in chunk:
                name, _, value = chunk.partition("=")
                jar[name.strip()] = value.strip()
        return jar

    def _request(self, method: str, path: str, **kwargs) -> dict:
        params = kwargs.pop("params", {}) or {}
        params.setdefault("access_token", self.access_token)
        try:
            with httpx.Client(**self._client_kwargs()) as client:
                response = client.request(method, self._url(path), params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise FacebookError(
                f"Facebook недоступен: {exc}", kind="transport"
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code >= 400 or "error" in body:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise FacebookError(
                error.get("message") or f"Facebook вернул {response.status_code}",
                kind=error.get("type", ""),
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

    ACCOUNT_FIELDS = "id,account_id,name,account_status,timezone_name,currency"

    def _collect(self, path: str, params: dict, max_pages: int = 25) -> list[dict]:
        """Проходит постранично по ребру Graph API."""
        out: list[dict] = []
        page = 0
        while True:
            body = self._request("GET", path, params=params)
            out.extend(body.get("data", []))
            page += 1
            paging = body.get("paging", {}) or {}
            after = (paging.get("cursors", {}) or {}).get("after")
            if not after or not paging.get("next") or page >= max_pages:
                break
            params = dict(params, after=after)
        return out

    def describe_token(self) -> dict:
        """Что это за токен: профиль или системный пользователь, с какими правами.

        У системного пользователя нет ребра me/permissions — по нему и различаем.
        """
        info: dict = {
            "name": "", "id": "", "permissions": [], "kind": "неизвестно",
            "error": "", "hint": "",
        }
        try:
            me = self._request("GET", "me", params={"fields": "id,name"})
        except FacebookError as exc:
            info["error"] = str(exc)
            info["hint"] = exc.hint
            return info

        info["id"] = me.get("id", "")
        info["name"] = me.get("name", "")
        try:
            granted = self._request("GET", "me/permissions", params={"limit": 200}).get("data", [])
            info["permissions"] = [
                p["permission"] for p in granted if p.get("status") == "granted"
            ]
            info["kind"] = "токен профиля"
        except FacebookError:
            info["kind"] = "токен системного пользователя"
        return info

    def list_accounts(self) -> list[dict]:
        """Кабинеты, доступные токену социального аккаунта."""
        return self.list_accounts_verbose()[0]

    def list_accounts_verbose(self) -> tuple[list[dict], list[str]]:
        """Кабинеты и заметки о том, как они искались.

        Основной путь — me/adaccounts: так отдаёт кабинеты токен профиля, ради
        которого всё и затевалось. Если это оказался токен системного
        пользователя, такого ребра у него нет — тогда пробуем через бизнес.
        """
        notes: list[str] = []
        found: dict[str, dict] = {}

        try:
            for row in self._collect(
                "me/adaccounts", {"fields": self.ACCOUNT_FIELDS, "limit": 200}
            ):
                found[str(row.get("account_id") or row.get("id"))] = row
        except FacebookError as exc:
            notes.append(f"me/adaccounts: {exc}")
            if exc.hint:
                notes.append(f"  {exc.hint}")

        if found:
            return list(found.values()), notes

        try:
            businesses = self._collect("me/businesses", {"fields": "id,name", "limit": 100})
        except FacebookError as exc:
            notes.append(f"me/businesses: {exc}")
            businesses = []

        for business in businesses:
            for edge in ("owned_ad_accounts", "client_ad_accounts"):
                try:
                    rows = self._collect(
                        f"{business['id']}/{edge}",
                        {"fields": self.ACCOUNT_FIELDS, "limit": 200},
                    )
                except FacebookError as exc:
                    notes.append(f"{business.get('name') or business['id']}/{edge}: {exc}")
                    continue
                if rows:
                    notes.append(
                        f"{business.get('name') or business['id']} · {edge}: {len(rows)}"
                    )
                for row in rows:
                    found[str(row.get("account_id") or row.get("id"))] = row

        return list(found.values()), notes

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

        return self._collect(_act(account_id) + "/adsets", params)

    def set_adset_status(self, adset_id: str, status: str) -> bool:
        self._request("POST", adset_id, params={"status": status})
        return True

    def pause_adset(self, adset_id: str) -> bool:
        return self.set_adset_status(adset_id, STATUS_PAUSED)

    def resume_adset(self, adset_id: str) -> bool:
        return self.set_adset_status(adset_id, STATUS_ACTIVE)

    def check_token(self) -> dict:
        return self._request("GET", "me", params={"fields": "id,name"})

    # ------------------------------------------------------------ прокси и куки

    def check_proxy(self) -> str:
        """С какого IP Facebook видит наши запросы. Пустая строка — не узнали."""
        try:
            with httpx.Client(**self._client_kwargs()) as client:
                response = client.get("https://api.ipify.org", params={"format": "json"})
                if response.status_code < 400:
                    return str(response.json().get("ip", ""))
        except (httpx.HTTPError, ValueError) as exc:
            raise FacebookError(
                f"прокси не работает: {exc}", kind="transport"
            ) from exc
        return ""

    def refresh_token_from_cookies(self) -> str:
        """Достаёт свежий токен из залогиненной сессии.

        Способ держится на вёрстке страниц Facebook и может отвалиться, когда
        они её поменяют. Это запасной путь на случай протухшего токена, а не
        основной механизм: пустая строка означает «не вышло, нужен человек».
        """
        if not self.cookies:
            return ""

        jar = self._cookie_jar()
        for url in _TOKEN_SOURCES:
            try:
                with httpx.Client(**self._client_kwargs()) as client:
                    response = client.get(url, cookies=jar)
            except httpx.HTTPError as exc:
                log.debug("не удалось открыть %s: %s", url, exc)
                continue
            if response.status_code >= 400:
                continue
            body = response.text
            for pattern in _TOKEN_PATTERNS:
                found = pattern.search(body)
                if found:
                    token = found.group(1) if found.groups() else found.group(0)
                    log.info("токен перевыпущен из кук через %s", url)
                    return token
        return ""


def client_for_social(social, settings) -> FacebookClient:
    """Строит клиент из социального аккаунта: токен, куки, прокси, user-agent."""
    return FacebookClient(
        access_token=social.access_token,
        api_version=settings.fb_api_version,
        timeout=settings.fb_timeout,
        proxy=social.proxy,
        cookies=social.cookies,
        user_agent=social.user_agent,
    )


def _act(account_id: str) -> str:
    account_id = str(account_id).strip()
    return account_id if account_id.startswith("act_") else f"act_{account_id}"
