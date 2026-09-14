"""Веб-приложение автоконтроля: вход, пороги, адсеты, журнал решений."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session, init_db, session_scope
from app.deps import current_user, current_user_optional, require_global_editor
from app.clients.facebook import FacebookError, client_for_social
from app.engine import AutocontrolEngine
from app.multitoken import MultitokenError, normalize_proxy, parse_multitoken
from app.formatting import integer, money, rate, since, when
from app.models import (
    DECISION_TITLES, ROLE_TITLES, SOURCE_TITLES, STATE_OFF, STATE_PAUSED,
    STATE_RELEASED, STATE_TITLES, STATE_WATCHING, AdAccount, ControlledAdset,
    Decision, GeoThreshold, KeitaroProfile, SocialAccount, User, UserGeoThreshold,
)
from app.rules import RULE_TITLES, all_breaches
from app.security import SESSION_COOKIE, issue_session
from app.services import (
    ServiceError, attach_adset, authenticate, controlled_count_by_geo,
    create_user, decisions_count, delete_ad_account, delete_adset,
    delete_keitaro_profile, delete_social_account, delete_user_threshold,
    detach_adset, mark_human_resume, normalize_geo, resolve_thresholds,
    under_control_count, upsert_geo_threshold, upsert_user_threshold,
)
from app.tzwindow import UnknownTimezone, describe_offset, resolve_tz

log = logging.getLogger(__name__)
UTC = timezone.utc
settings = get_settings()

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["money"] = money
templates.env.filters["rate"] = rate
templates.env.filters["integer"] = integer
templates.env.filters["when"] = when
templates.env.filters["since"] = since
templates.env.globals["RULE_TITLES"] = RULE_TITLES
templates.env.globals["STATE_TITLES"] = STATE_TITLES
templates.env.globals["DECISION_TITLES"] = DECISION_TITLES
templates.env.globals["SOURCE_TITLES"] = SOURCE_TITLES
templates.env.globals["ROLE_TITLES"] = ROLE_TITLES
templates.env.globals["RECOVERY_HOURS"] = settings.recovery_hours
templates.env.globals["PROXY_STATUS"] = {
    "ok": "РАБОТАЕТ",
    "unknown": "НЕ ПРОВЕРЕН",
    "invalid": "НЕ РАБОТАЕТ",
}
templates.env.globals["TOKEN_STATUS"] = {
    "ok": "ЖИВОЙ",
    "unknown": "НЕ ПРОВЕРЕН",
    "invalid": "НЕ РАБОТАЕТ",
    "no_perms": "НЕТ ПРАВ",
    "no_accounts": "НЕТ КАБИНЕТОВ",
}


def bootstrap_admin() -> None:
    """Создаёт первого пользователя, если база пустая."""
    if not settings.bootstrap_password:
        return
    with session_scope() as session:
        if session.scalar(select(User).limit(1)) is not None:
            return
        create_user(
            session,
            settings.bootstrap_login,
            settings.bootstrap_password,
            role=settings.bootstrap_role,
        )
        log.info("создан первый пользователь: %s", settings.bootstrap_login)


async def _engine_loop() -> None:  # pragma: no cover - фоновая задача
    engine = AutocontrolEngine()
    while True:
        try:
            await asyncio.to_thread(_tick_once, engine)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("тик автоконтроля упал")
        await asyncio.sleep(settings.tick_seconds)


def _tick_once(engine: AutocontrolEngine) -> None:
    with session_scope() as session:
        report = engine.tick(session)
    if report.checked:
        log.info("тик: %s", report.as_dict())


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_admin()
    task = None
    if settings.run_engine_in_web:
        task = asyncio.create_task(_engine_loop())
        log.info("движок автоконтроля поднят внутри веб-процесса")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Автоконтроль адсетов", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _wants_json(request: Request) -> bool:
    """JSON отдаём только тому, кто прямо о нём попросил."""
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.exception_handler(HTTPException)
async def _auth_redirect(request: Request, exc: HTTPException):
    """Неавторизованного отправляем на форму входа, а не отдаём голый 401."""
    if _wants_json(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        nxt = request.url.path
        suffix = f"?next={nxt}" if nxt and nxt != "/" else ""
        return RedirectResponse(f"/login{suffix}", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "error.html", {"detail": exc.detail, "code": exc.status_code},
        status_code=exc.status_code,
    )


# --------------------------------------------------------------------------- #
#  Вход
# --------------------------------------------------------------------------- #

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: User | None = Depends(current_user_optional)):
    if user is not None:
        return RedirectResponse("/autocontrol", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html", {"next": request.query_params.get("next", "/autocontrol")}
    )


@app.post("/login")
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/autocontrol"),
    session: Session = Depends(get_session),
):
    user = authenticate(session, login, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль", "next": next, "login": login},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    session.commit()
    target = next if next.startswith("/") else "/autocontrol"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE, issue_session(user.id), httponly=True, samesite="lax",
        path="/", secure=settings.cookie_secure,
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/")
def root(user: User | None = Depends(current_user_optional)):
    target = "/autocontrol" if user else "/login"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
#  Автоконтроль
# --------------------------------------------------------------------------- #

def _adset_view(adset: ControlledAdset, now: datetime) -> dict:
    """Строка таблицы «Адсеты под контролем»."""
    metrics = adset.last_metrics()
    thresholds = adset.thresholds()
    account = adset.account
    return {
        "row": adset,
        "metrics": metrics,
        "thresholds": thresholds,
        "breaches": {b.rule for b in all_breaches(metrics, thresholds)},
        "account_title": account.title or account.account_id if account else "—",
        "account_tz": describe_offset(account.timezone_name, now) if account else "—",
        "paused_for": since(adset.paused_at, now) if adset.paused_at else "—",
        "rule_title": RULE_TITLES.get(adset.paused_rule or "", ""),
    }


@app.get("/autocontrol", response_class=HTMLResponse)
def autocontrol_page(
    request: Request,
    tab: str = "adsets",
    kind: str = "",
    rule: str = "",
    group: str = "",
    limit: int = 200,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    now = datetime.now(UTC)

    geo_rows = session.scalars(select(GeoThreshold).order_by(GeoThreshold.geo)).all()
    my_rows = session.scalars(
        select(UserGeoThreshold)
        .where(UserGeoThreshold.user_id == user.id)
        .order_by(UserGeoThreshold.geo)
    ).all()
    counts = controlled_count_by_geo(session)

    adsets = session.scalars(
        select(ControlledAdset)
        .where(ControlledAdset.state != STATE_OFF)
        .order_by(
            # Сначала выключенные — за ними и следим.
            ControlledAdset.state != STATE_PAUSED,
            desc(ControlledAdset.paused_at),
            ControlledAdset.name,
        )
    ).all()
    views = [_adset_view(a, now) for a in adsets]
    accounts = session.scalars(select(AdAccount).order_by(AdAccount.title)).all()

    by_campaign: dict[str, list[dict]] = {}
    if group == "campaign":
        for view in views:
            key = view["row"].campaign_name or "— без кампании —"
            by_campaign.setdefault(key, []).append(view)

    query = select(Decision).order_by(desc(Decision.created_at))
    if kind:
        query = query.where(Decision.kind == kind)
    if rule:
        query = query.where(Decision.rule == rule)
    decisions = session.scalars(query.limit(max(1, min(limit, 1000)))).all()

    return templates.TemplateResponse(
        request,
        "autocontrol.html",
        {
            "user": user,
            "tab": tab,
            "geo_rows": geo_rows,
            "my_rows": my_rows,
            "counts": counts,
            "views": views,
            "accounts": accounts,
            "by_campaign": by_campaign,
            "group": group,
            "decisions": decisions,
            "filter_kind": kind,
            "filter_rule": rule,
            "limit": limit,
            "under_control": under_control_count(session),
            "decisions_total": decisions_count(session),
            "now": now,
            "settings": settings,
            "STATE_WATCHING": STATE_WATCHING,
            "STATE_PAUSED": STATE_PAUSED,
            "STATE_RELEASED": STATE_RELEASED,
        },
    )


def _decimal(raw: str | None) -> float | None:
    """Пустое поле — правило выключено; запятая как разделитель тоже принимается."""
    if raw is None:
        return None
    raw = raw.strip().replace(",", ".").replace("$", "").replace(" ", "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ServiceError(f"не число: {raw}") from exc
    if value < 0:
        raise ServiceError("порог не может быть отрицательным")
    return value or None


def _back(tab: str = "thresholds") -> RedirectResponse:
    return RedirectResponse(f"/autocontrol?tab={tab}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/thresholds/global")
def save_global_threshold(
    geo: str = Form(...),
    max_cpa: str = Form(""),
    max_spend_no_conv: str = Form(""),
    max_ucpc: str = Form(""),
    user: User = Depends(require_global_editor),
    session: Session = Depends(get_session),
):
    try:
        upsert_geo_threshold(
            session, geo, _decimal(max_cpa), _decimal(max_spend_no_conv),
            _decimal(max_ucpc), user=user,
        )
        session.commit()
    except ServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _back("thresholds")


@app.post("/thresholds/mine")
def save_my_threshold(
    geo: str = Form(...),
    max_cpa: str = Form(""),
    max_spend_no_conv: str = Form(""),
    max_ucpc: str = Form(""),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    try:
        upsert_user_threshold(
            session, user.id, geo, _decimal(max_cpa), _decimal(max_spend_no_conv),
            _decimal(max_ucpc),
        )
        session.commit()
    except ServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _back("thresholds")


@app.post("/thresholds/mine/delete")
def drop_my_threshold(
    geo: str = Form(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    delete_user_threshold(session, user.id, geo)
    session.commit()
    return _back("thresholds")


def _get_adset(session: Session, adset_pk: int) -> ControlledAdset:
    adset = session.get(ControlledAdset, adset_pk)
    if adset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "адсет не найден")
    return adset


@app.post("/adsets/{adset_pk}/detach")
def detach(
    adset_pk: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    detach_adset(session, _get_adset(session, adset_pk), actor=user.login)
    session.commit()
    return _back("adsets")


@app.post("/adsets/{adset_pk}/delete")
def remove(
    adset_pk: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    delete_adset(session, _get_adset(session, adset_pk))
    session.commit()
    return _back("adsets")


@app.post("/adsets/{adset_pk}/enable")
def enable_by_human(
    adset_pk: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Кнопка «Включить»: человек возвращает адсет в работу."""
    adset = _get_adset(session, adset_pk)
    account = adset.account
    engine = AutocontrolEngine()
    if not settings.dry_run and account is not None:
        try:
            engine.facebook_for(account).resume_adset(adset.adset_id)
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Facebook: {exc}") from exc
    mark_human_resume(session, adset, actor=user.login)
    session.commit()
    return _back("adsets")


@app.post("/adsets/attach")
def attach(
    adset_id: str = Form(...),
    account_pk: int = Form(...),
    geo: str = Form(...),
    name: str = Form(""),
    campaign_name: str = Form(""),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Постановка под контроль — то, что при заливе делается автоматически."""
    account = session.get(AdAccount, account_pk)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "кабинет не найден")
    try:
        attach_adset(
            session, adset_id=adset_id, account=account, geo=geo, name=name,
            campaign_name=campaign_name, owner=user,
        )
        session.commit()
    except ServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _back("adsets")


# --------------------------------------------------------------------------- #
#  Интеграции
# --------------------------------------------------------------------------- #

@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    now = datetime.now(UTC)
    accounts = session.scalars(select(AdAccount).order_by(AdAccount.title)).all()
    return templates.TemplateResponse(
        request,
        "integrations.html",
        {
            "user": user,
            "keitaro_profiles": session.scalars(select(KeitaroProfile)).all(),
            "socials": session.scalars(select(SocialAccount)).all(),
            "accounts": accounts,
            "offsets": {a.id: describe_offset(a.timezone_name, now) for a in accounts},
            "settings": settings,
            "now": now,
        },
    )


@app.post("/integrations/keitaro")
def save_keitaro(
    title: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(...),
    timezone_name: str = Form("Europe/Moscow"),
    adset_field: str = Form("sub_id_6"),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    session.add(
        KeitaroProfile(
            title=title.strip(),
            base_url=base_url.strip(),
            api_key=api_key.strip(),
            timezone_name=timezone_name.strip() or "Europe/Moscow",
            adset_field=adset_field.strip() or "sub_id_6",
        )
    )
    session.commit()
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/integrations/keitaro/{profile_id}")
def update_keitaro(
    profile_id: int,
    title: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    timezone_name: str = Form("Europe/Moscow"),
    adset_field: str = Form("sub_id_6"),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Правка трекера. Поле с ID адсета здесь меняется чаще всего."""
    profile = session.get(KeitaroProfile, profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "трекер не найден")
    profile.title = title.strip() or profile.title
    profile.base_url = base_url.strip() or profile.base_url
    profile.timezone_name = timezone_name.strip() or profile.timezone_name
    profile.adset_field = adset_field.strip() or profile.adset_field
    if api_key.strip():
        # Пустое поле оставляет ключ прежним, чтобы не перевводить его ради поля.
        profile.api_key = api_key.strip()
    session.commit()
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


def _apply_multitoken(social: SocialAccount, raw: str, proxy_override: str = "") -> None:
    """Разбирает мультитокен и раскладывает его по полям аккаунта."""
    try:
        parsed = parse_multitoken(raw)
    except MultitokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if parsed.access_token:
        social.access_token = parsed.access_token
    if parsed.cookies:
        social.cookies = parsed.cookies
    if parsed.user_agent:
        social.user_agent = parsed.user_agent
    if parsed.uid:
        social.fb_user_id = parsed.uid

    proxy = proxy_override.strip()
    if proxy:
        try:
            social.proxy = normalize_proxy(proxy)
        except MultitokenError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    elif parsed.proxy:
        social.proxy = parsed.proxy

    social.token_status = "unknown"
    social.token_error = ""
    social.proxy_status = "unknown"


@app.post("/integrations/social")
def save_social(
    title: str = Form(...),
    multitoken: str = Form(...),
    proxy: str = Form(""),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Мультитокен: токен, куки, прокси и user-agent одной вставкой."""
    social = SocialAccount(title=title.strip())
    _apply_multitoken(social, multitoken, proxy)
    session.add(social)
    session.commit()
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/integrations/social/{social_id}")
def update_social(
    social_id: int,
    title: str = Form(""),
    multitoken: str = Form(""),
    proxy: str = Form(""),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Правка аккаунта. Пустой мультитокен оставляет прежние секреты."""
    social = session.get(SocialAccount, social_id)
    if social is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "аккаунт не найден")
    if title.strip():
        social.title = title.strip()
    if multitoken.strip():
        _apply_multitoken(social, multitoken, proxy)
    elif proxy.strip():
        try:
            social.proxy = normalize_proxy(proxy)
        except MultitokenError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        social.proxy_status = "unknown"
    session.commit()
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/integrations/social/{social_id}/check")
def check_social(
    social_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Проверка аккаунта: жив ли прокси, жив ли токен, и починка из кук."""
    social = session.get(SocialAccount, social_id)
    if social is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "аккаунт не найден")

    engine = AutocontrolEngine()
    client = client_for_social(social, settings)

    if social.proxy:
        try:
            social.proxy_ip = client.check_proxy()
            social.proxy_status = "ok" if social.proxy_ip else "unknown"
        except FacebookError as exc:
            social.proxy_status = "invalid"
            social.token_error = str(exc)
            session.commit()
            return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)

    info = client.describe_token()
    if info["error"]:
        social.token_status = "invalid"
        # К сухому «Invalid request. (#1)» добавляем причину, если она понятна.
        social.token_error = (
            f"{info['error']} — {info['hint']}" if info.get("hint") else info["error"]
        )
        # Куки мультитокена — шанс поднять токен без человека.
        if engine.refresh_token(social):
            social.token_error = ""
    else:
        social.token_status = "ok"
        social.token_error = ""
        social.fb_user_id = social.fb_user_id or info["id"]
    social.token_checked_at = datetime.now(UTC)
    session.commit()
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/integrations/account")
def save_account(
    account_id: str = Form(...),
    title: str = Form(""),
    timezone_name: str = Form(""),
    social_id: int = Form(...),
    keitaro_id: int = Form(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    account_id = account_id.strip().removeprefix("act_")
    if not account_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указан ID кабинета")

    # Ссылки проверяем до записи: иначе внешний ключ роняет запрос пятисоткой.
    if session.get(SocialAccount, social_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "выбранный аккаунт не найден")
    if session.get(KeitaroProfile, keitaro_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "выбранный трекер не найден")

    row = session.scalar(select(AdAccount).where(AdAccount.account_id == account_id))
    if row is None:
        row = AdAccount(account_id=account_id)
        session.add(row)
    row.title = title.strip() or row.title
    row.social_id = social_id
    row.keitaro_id = keitaro_id
    row.timezone_name = timezone_name.strip()

    if row.timezone_name:
        try:
            resolve_tz(row.timezone_name)
        except UnknownTimezone as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Таймзону лучше взять у самого Facebook — по ней и отмеряются сутки.
    if not row.timezone_name:
        social = session.get(SocialAccount, social_id)
        if social is not None:
            from app.clients.facebook import FacebookClient, FacebookError

            try:
                data = FacebookClient(
                    social.access_token, settings.fb_api_version, settings.fb_timeout
                ).get_account(account_id)
                row.timezone_name = data.get("timezone_name", "")
                row.title = row.title or data.get("name", "")
                row.currency = data.get("currency", "USD")
                row.synced_at = datetime.now(UTC)
            except FacebookError as exc:
                session.commit()
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"не удалось получить таймзону кабинета: {exc}",
                ) from exc
    session.commit()
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
#  Удаление из интеграций
# --------------------------------------------------------------------------- #

def _to_integrations() -> RedirectResponse:
    return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/integrations/keitaro/{profile_id}/delete")
def remove_keitaro(
    profile_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    profile = session.get(KeitaroProfile, profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "трекер не найден")
    try:
        delete_keitaro_profile(session, profile)
        session.commit()
    except ServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _to_integrations()


@app.post("/integrations/social/{social_id}/delete")
def remove_social(
    social_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    social = session.get(SocialAccount, social_id)
    if social is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "аккаунт не найден")
    try:
        delete_social_account(session, social)
        session.commit()
    except ServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _to_integrations()


@app.post("/integrations/account/{account_pk}/delete")
def remove_ad_account(
    account_pk: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Удаляет кабинет вместе с его адсетами. В Facebook ничего не меняется."""
    account = session.get(AdAccount, account_pk)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "кабинет не найден")
    removed = delete_ad_account(session, account)
    session.commit()
    log.info("удалён кабинет %s вместе с %s адсетами", account.account_id, removed)
    return _to_integrations()


@app.post("/engine/run")
def run_engine_now(
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    report = AutocontrolEngine().tick(session)
    if _wants_json(request):
        return JSONResponse(report.as_dict())
    return _back("adsets")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
