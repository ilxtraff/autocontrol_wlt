"""Разбор мультитокена социального аккаунта.

Мультитокен — это связка «токен + куки + прокси + user-agent», выгружаемая из
антидетект-браузеров и сервисов аккаунтов. Единого формата нет: встречаются
`токен|куки`, `uid|пароль|токен|куки|ua`, JSON, куки массивом объектов из
расширения браузера. Поэтому куски определяются по виду, а не по позиции —
так один разбор переваривает все распространённые выгрузки.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field

# Токены Graph API начинаются с EAA; у страничных и системных префикс тот же.
_TOKEN_RE = re.compile(r"\bEAA[A-Za-z0-9_\-]{20,}\b")
_UID_RE = re.compile(r"^\d{5,25}$")
_PROXY_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*)://", re.I)
# httpx умеет только эти схемы; socks4 он отвергает уже на создании клиента,
# поэтому отсеиваем его здесь, а не в момент первого запроса.
_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_HOSTPORT_RE = re.compile(r"^[\w.\-]+:\d{2,5}$")
_HOSTPORT_AUTH_RE = re.compile(r"^[\w.\-]+:\d{2,5}:[^:]+:[^:]+$")
_AUTH_HOSTPORT_RE = re.compile(r"^[^:@/]+:[^:@/]*@[\w.\-]+:\d{2,5}$")

# Куки, по которым узнаётся сессия Facebook.
_COOKIE_MARKERS = ("c_user=", "xs=", "datr=", "sb=", "fr=")

# Хранить имеет смысл только то, что реально нужно для сессии.
_COOKIE_KEEP = {"c_user", "xs", "datr", "sb", "fr", "presence", "wd", "dpr", "locale"}


class MultitokenError(ValueError):
    """Мультитокен не удалось разобрать."""


def _maybe_base64_json(text: str):
    """Снимает base64-обёртку, если под ней JSON.

    Антидетект-браузеры и сервисы аккаунтов часто отдают мультитокен как
    base64 от JSON `{"cookies": [...], "ua": "...", "token": "..."}`. Обычный
    токен или строка кук под это условие не попадают: они либо не base64,
    либо декодируются не в JSON.
    """
    stripped = text.strip()
    # base64 не содержит пробелов, кавычек, скобок и разделителей мультитокена.
    _forbidden = set(" \t\r\n\"{}|;")
    if len(stripped) < 40 or any(ch in _forbidden for ch in stripped):
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/_\-]+={0,2}", stripped):
        return None
    padded = stripped + "=" * (-len(stripped) % 4)
    for altchars in (None, b"-_"):
        try:
            decoded = base64.b64decode(padded, altchars=altchars, validate=False)
            data = json.loads(decoded)
        except (binascii.Error, ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            # Экспорт одних кук массивом, без обёртки.
            return {"cookies": data}
    return None


def _derive_uid(result: "Multitoken") -> None:
    """Достаёт uid из куки c_user, если отдельно он не пришёл."""
    if result.uid or "c_user=" not in result.cookies:
        return
    for chunk in result.cookies.split("; "):
        if chunk.startswith("c_user="):
            result.uid = chunk.split("=", 1)[1]
            break


@dataclass
class Multitoken:
    access_token: str = ""
    cookies: str = ""
    user_agent: str = ""
    proxy: str = ""
    uid: str = ""
    unknown: list[str] = field(default_factory=list)

    @property
    def has_session(self) -> bool:
        """Есть ли куки, по которым можно перевыпустить токен."""
        return "c_user=" in self.cookies and "xs=" in self.cookies

    def describe(self) -> str:
        """Что разобралось — без выдачи самих секретов."""
        bits = []
        bits.append(f"токен {_mask(self.access_token)}" if self.access_token else "токена нет")
        if self.cookies:
            names = [c.split("=", 1)[0] for c in self.cookies.split("; ") if "=" in c]
            bits.append(f"куки: {', '.join(names[:8])}{'…' if len(names) > 8 else ''}")
        else:
            bits.append("кук нет")
        bits.append(f"прокси {_mask_proxy(self.proxy)}" if self.proxy else "прокси нет")
        if self.user_agent:
            bits.append(f"ua: {self.user_agent[:40]}…")
        if self.uid:
            bits.append(f"uid {self.uid}")
        return " · ".join(bits)


def _mask(value: str, keep: int = 6) -> str:
    if not value:
        return "—"
    return f"…{value[-keep:]}" if len(value) > keep else "…"


def _mask_proxy(proxy: str) -> str:
    """Прячет логин и пароль, оставляя хост и порт."""
    if not proxy:
        return "—"
    without_scheme = re.sub(r"^\w+://", "", proxy)
    if "@" in without_scheme:
        without_scheme = without_scheme.split("@", 1)[1]
    scheme = proxy.split("://", 1)[0] if "://" in proxy else "http"
    return f"{scheme}://{without_scheme}"


# --------------------------------------------------------------------------- #
#  Куки
# --------------------------------------------------------------------------- #

def normalize_cookies(raw: str | list | dict) -> str:
    """Приводит куки к строке `name=value; name=value`.

    Понимает строку заголовка Cookie, JSON-массив объектов из расширения
    браузера и словарь.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        if text.startswith("[") or text.startswith("{"):
            try:
                return normalize_cookies(json.loads(text))
            except json.JSONDecodeError:
                pass
        pairs = []
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name = name.strip()
            if name in _COOKIE_KEEP:
                pairs.append(f"{name}={value.strip()}")
        return "; ".join(pairs)

    if isinstance(raw, dict):
        # Либо {name: value}, либо один объект куки.
        if "name" in raw and "value" in raw:
            return normalize_cookies([raw])
        return "; ".join(
            f"{k}={v}" for k, v in raw.items() if k in _COOKIE_KEEP and v
        )

    if isinstance(raw, list):
        pairs = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            value = item.get("value", "")
            if name in _COOKIE_KEEP and value:
                pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    return ""


# --------------------------------------------------------------------------- #
#  Прокси
# --------------------------------------------------------------------------- #

def normalize_proxy(raw: str) -> str:
    """Приводит прокси к виду `scheme://user:pass@host:port`.

    Принимает `host:port`, `host:port:user:pass`, `user:pass@host:port`
    и любой из них уже со схемой.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    scheme = "http"
    match = _PROXY_SCHEME_RE.match(text)
    if match:
        scheme = match.group(1).lower()
        if scheme not in _PROXY_SCHEMES:
            raise MultitokenError(
                f"схема прокси {scheme} не поддерживается — нужны "
                f"{', '.join(sorted(_PROXY_SCHEMES))}"
            )
        text = text[match.end():]

    if _AUTH_HOSTPORT_RE.match(text):
        return f"{scheme}://{text}"
    if _HOSTPORT_AUTH_RE.match(text):
        host, port, user, password = text.split(":", 3)
        return f"{scheme}://{user}:{password}@{host}:{port}"
    if _HOSTPORT_RE.match(text):
        return f"{scheme}://{text}"

    raise MultitokenError(f"не понял формат прокси: {raw}")


def looks_like_proxy(part: str) -> bool:
    text = part.strip()
    match = _PROXY_SCHEME_RE.match(text)
    if match:
        return match.group(1).lower() in _PROXY_SCHEMES
    return bool(
        _AUTH_HOSTPORT_RE.match(text)
        or _HOSTPORT_AUTH_RE.match(text)
        or _HOSTPORT_RE.match(text)
    )


# --------------------------------------------------------------------------- #
#  Разбор
# --------------------------------------------------------------------------- #

def _from_mapping(data: dict) -> Multitoken:
    """JSON-выгрузка: ключи у всех называются по-разному."""
    def pick(*names: str):
        for name in names:
            for key in data:
                if key.lower().replace("-", "_") == name:
                    return data[key]
        return None

    token = pick("access_token", "token", "accesstoken", "eaag")
    cookies = pick("cookies", "cookie", "ck")
    agent = pick("user_agent", "useragent", "ua")
    proxy = pick("proxy", "proxies", "proxy_url")
    uid = pick("uid", "id", "user_id", "fb_id", "c_user")

    result = Multitoken(
        access_token=str(token).strip() if token else "",
        cookies=normalize_cookies(cookies) if cookies else "",
        user_agent=str(agent).strip() if agent else "",
        uid=str(uid).strip() if uid else "",
    )
    if proxy:
        result.proxy = normalize_proxy(str(proxy))
    _derive_uid(result)
    return result


def parse_multitoken(raw: str) -> Multitoken:
    """Разбирает мультитокен любого из распространённых форматов."""
    text = (raw or "").strip()
    if not text:
        raise MultitokenError("пустой мультитокен")

    # base64(JSON) — выгрузка из антидетект-браузеров и сервисов аккаунтов.
    unwrapped = _maybe_base64_json(text)
    if unwrapped is not None:
        result = _from_mapping(unwrapped)
        _require_something(result)
        return result

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MultitokenError(f"похоже на JSON, но разобрать не вышло: {exc}") from exc
        if isinstance(data, dict):
            result = _from_mapping(data)
            _require_something(result)
            return result

    # Разделителем бывает и `|`, и перевод строки.
    parts = [p.strip() for p in re.split(r"[|\n]", text) if p.strip()]
    result = Multitoken()

    for part in parts:
        if not result.access_token:
            found = _TOKEN_RE.search(part)
            if found:
                result.access_token = found.group(0)
                # Кусок мог быть только токеном — тогда дальше его не разбираем.
                if part.strip() == result.access_token:
                    continue

        if any(marker in part for marker in _COOKIE_MARKERS):
            cookies = normalize_cookies(part)
            if cookies:
                result.cookies = cookies
                continue

        if "Mozilla/" in part:
            result.user_agent = part
            continue

        if _UID_RE.match(part):
            result.uid = result.uid or part
            continue

        if looks_like_proxy(part):
            try:
                result.proxy = normalize_proxy(part)
            except MultitokenError:
                result.unknown.append(part)
            continue

        if part != result.access_token:
            result.unknown.append(part)

    _derive_uid(result)
    _require_something(result)
    return result


def _require_something(result: Multitoken) -> None:
    if not result.access_token and not result.has_session:
        raise MultitokenError(
            "не нашёл ни токена, ни кук сессии (нужны c_user и xs) — "
            "проверьте, что скопировали мультитокен целиком"
        )
