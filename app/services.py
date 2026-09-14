"""Прикладная логика поверх моделей: пороги, постановка адсетов под контроль."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    DECISION_DETACHED, DECISION_HUMAN_RESUMED, ROLE_BUYER, SOURCE_GLOBAL,
    SOURCE_MANUAL, SOURCE_PERSONAL, STATE_OFF, STATE_PAUSED, STATE_RELEASED,
    STATE_WATCHING, AdAccount, ControlledAdset, Decision, GeoThreshold,
    KeitaroProfile, SocialAccount, User, UserGeoThreshold, utcnow,
)
from app.rules import Thresholds
from app.security import hash_password


class ServiceError(RuntimeError):
    """Ошибка прикладного уровня — показывается пользователю как есть."""


# --------------------------------------------------------------------------- #
#  Пользователи
# --------------------------------------------------------------------------- #

def create_user(
    session: Session,
    login: str,
    password: str,
    *,
    role: str = ROLE_BUYER,
    display_name: str = "",
    team: str = "",
) -> User:
    login = login.strip().lower()
    if not login:
        raise ServiceError("пустой логин")
    if len(password) < 6:
        raise ServiceError("пароль короче 6 символов")
    exists = session.scalar(select(User).where(User.login == login))
    if exists is not None:
        raise ServiceError(f"пользователь {login} уже есть")
    user = User(
        login=login,
        password_hash=hash_password(password),
        role=role,
        display_name=display_name or login.upper(),
        team=team,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, login: str, password: str) -> User | None:
    from app.security import verify_password

    user = session.scalar(select(User).where(User.login == login.strip().lower()))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow()
    return user


# --------------------------------------------------------------------------- #
#  Пороги
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ResolvedThresholds:
    thresholds: Thresholds
    source: str

    @property
    def is_empty(self) -> bool:
        return self.thresholds.is_empty()


def normalize_geo(geo: str) -> str:
    geo = (geo or "").strip().upper()
    if len(geo) != 2 or not geo.isalpha():
        raise ServiceError(f"гео должно быть двумя буквами, получено: {geo or '—'}")
    return geo


def resolve_thresholds(session: Session, geo: str, user_id: int | None) -> ResolvedThresholds:
    """Свой порог по гео, иначе общий дефолт команды.

    Ровно как в CRM: «Не выбрали — подставится общий дефолт по гео, поэтому
    вести весь список необязательно».
    """
    geo = normalize_geo(geo)

    if user_id is not None:
        personal = session.scalar(
            select(UserGeoThreshold).where(
                UserGeoThreshold.user_id == user_id, UserGeoThreshold.geo == geo
            )
        )
        if personal is not None and not personal.as_thresholds().is_empty():
            return ResolvedThresholds(personal.as_thresholds(), SOURCE_PERSONAL)

    common = session.scalar(select(GeoThreshold).where(GeoThreshold.geo == geo))
    if common is not None and not common.as_thresholds().is_empty():
        return ResolvedThresholds(common.as_thresholds(), SOURCE_GLOBAL)

    return ResolvedThresholds(Thresholds(), SOURCE_GLOBAL)


def upsert_geo_threshold(
    session: Session,
    geo: str,
    max_cpa: float | None,
    max_spend_no_conv: float | None,
    max_ucpc: float | None,
    *,
    user: User,
) -> GeoThreshold:
    if not user.can_edit_global_thresholds:
        raise ServiceError("общие пороги меняют только admin и group CEO")
    geo = normalize_geo(geo)
    row = session.scalar(select(GeoThreshold).where(GeoThreshold.geo == geo))
    if row is None:
        row = GeoThreshold(geo=geo)
        session.add(row)
    row.max_cpa = max_cpa
    row.max_spend_no_conv = max_spend_no_conv
    row.max_ucpc = max_ucpc
    row.updated_by_id = user.id
    row.updated_at = utcnow()
    session.flush()
    return row


def upsert_user_threshold(
    session: Session,
    user_id: int,
    geo: str,
    max_cpa: float | None,
    max_spend_no_conv: float | None,
    max_ucpc: float | None,
) -> UserGeoThreshold:
    geo = normalize_geo(geo)
    row = session.scalar(
        select(UserGeoThreshold).where(
            UserGeoThreshold.user_id == user_id, UserGeoThreshold.geo == geo
        )
    )
    if row is None:
        row = UserGeoThreshold(user_id=user_id, geo=geo)
        session.add(row)
    row.max_cpa = max_cpa
    row.max_spend_no_conv = max_spend_no_conv
    row.max_ucpc = max_ucpc
    row.updated_at = utcnow()
    session.flush()
    return row


def delete_user_threshold(session: Session, user_id: int, geo: str) -> None:
    """Убрать гео из моего списка — заливы по нему пойдут по общему дефолту."""
    row = session.scalar(
        select(UserGeoThreshold).where(
            UserGeoThreshold.user_id == user_id, UserGeoThreshold.geo == normalize_geo(geo)
        )
    )
    if row is not None:
        session.delete(row)


def controlled_count_by_geo(session: Session) -> dict[str, int]:
    """Сколько адсетов под контролем по каждому гео — колонка «Под контролем»."""
    rows = session.execute(
        select(ControlledAdset.geo, func.count(ControlledAdset.id))
        .where(ControlledAdset.state.in_([STATE_WATCHING, STATE_PAUSED]))
        .group_by(ControlledAdset.geo)
    ).all()
    return {geo: count for geo, count in rows}


# --------------------------------------------------------------------------- #
#  Адсеты под контролем
# --------------------------------------------------------------------------- #

def attach_adset(
    session: Session,
    *,
    adset_id: str,
    account: AdAccount,
    geo: str,
    name: str = "",
    campaign_id: str = "",
    campaign_name: str = "",
    owner: User | None = None,
    thresholds: Thresholds | None = None,
) -> ControlledAdset:
    """Ставит адсет под автоконтроль, копируя в него пороги.

    Снимок делается один раз: правка справочника влияет на новые заливы,
    а идущие доживают по своим числам.
    """
    adset_id = (adset_id or "").strip()
    if not adset_id:
        raise ServiceError("не указан ID адсета")
    geo = normalize_geo(geo)

    if thresholds is not None and not thresholds.is_empty():
        source = SOURCE_MANUAL
    else:
        resolved = resolve_thresholds(session, geo, owner.id if owner else None)
        if resolved.is_empty:
            raise ServiceError(
                f"для гео {geo} не задано ни одного порога — заполните общие пороги или свои"
            )
        thresholds, source = resolved.thresholds, resolved.source

    row = session.scalar(select(ControlledAdset).where(ControlledAdset.adset_id == adset_id))
    if row is None:
        row = ControlledAdset(adset_id=adset_id)
        session.add(row)

    row.name = name or row.name or adset_id
    row.campaign_id = campaign_id or row.campaign_id
    row.campaign_name = campaign_name or row.campaign_name
    row.geo = geo
    row.account_pk = account.id
    row.owner_id = owner.id if owner else None
    row.max_cpa = thresholds.max_cpa
    row.max_spend_no_conv = thresholds.max_spend_no_conv
    row.max_ucpc = thresholds.max_ucpc
    row.threshold_source = source
    row.state = STATE_WATCHING
    row.paused_at = None
    row.paused_rule = None
    row.control_day = None
    row.released_at = None
    row.last_error = ""
    session.flush()
    return row


def detach_adset(session: Session, adset: ControlledAdset, actor: str) -> None:
    """«Снять»: адсет уходит из-под контроля, его статус в Facebook не трогаем."""
    from app.engine import record_decision

    adset.state = STATE_OFF
    adset.paused_rule = None
    adset.control_day = None
    record_decision(
        session, adset, DECISION_DETACHED, metrics=adset.last_metrics(),
        note="снят с автоконтроля вручную, статус адсета не менялся", actor=actor,
    )


def delete_adset(session: Session, adset: ControlledAdset) -> None:
    """Удалить из автоконтроля насовсем — вместе с журналом решений."""
    session.delete(adset)


def mark_human_resume(session: Session, adset: ControlledAdset, actor: str) -> None:
    """Человек включил адсет руками — возвращаем его под наблюдение."""
    from app.engine import NOTE_HUMAN, record_decision

    adset.state = STATE_WATCHING
    adset.paused_at = None
    adset.paused_rule = None
    adset.control_day = None
    adset.released_at = None
    record_decision(
        session, adset, DECISION_HUMAN_RESUMED, metrics=adset.last_metrics(),
        note=NOTE_HUMAN, actor=actor,
    )


# --------------------------------------------------------------------------- #
#  Удаление из интеграций
# --------------------------------------------------------------------------- #

def _accounts_using(session: Session, **filters) -> list[AdAccount]:
    query = select(AdAccount)
    for field, value in filters.items():
        query = query.where(getattr(AdAccount, field) == value)
    return list(session.scalars(query.order_by(AdAccount.title)))


def _blocked_by(accounts: list[AdAccount]) -> str:
    names = [a.title or a.account_id for a in accounts[:5]]
    tail = f" и ещё {len(accounts) - 5}" if len(accounts) > 5 else ""
    return ", ".join(names) + tail


def delete_keitaro_profile(session: Session, profile: KeitaroProfile) -> None:
    """Удаляет трекер. Отказывает, пока на нём висят кабинеты.

    Молча отвязать их нельзя: кабинет без трекера перестаёт считаться, и
    автоконтроль по нему встанет без единого следа в журнале.
    """
    used = _accounts_using(session, keitaro_id=profile.id)
    if used:
        raise ServiceError(
            f"трекер используют кабинеты: {_blocked_by(used)}. "
            "Переключите их на другой трекер или удалите сначала их"
        )
    session.delete(profile)


def delete_social_account(session: Session, social: SocialAccount) -> None:
    """Удаляет социальный аккаунт вместе с токеном, куками и прокси."""
    used = _accounts_using(session, social_id=social.id)
    if used:
        raise ServiceError(
            f"аккаунт управляет кабинетами: {_blocked_by(used)}. "
            "Переключите их на другой аккаунт или удалите сначала их"
        )
    session.delete(social)


def delete_ad_account(session: Session, account: AdAccount) -> int:
    """Удаляет кабинет вместе с его адсетами и журналом. Возвращает число адсетов.

    Сами адсеты в Facebook не трогаются — из базы уходит только то, что
    относится к автоконтролю.
    """
    adsets = list(
        session.scalars(select(ControlledAdset).where(ControlledAdset.account_pk == account.id))
    )
    for adset in adsets:
        # Журнал уходит вместе с адсетом: у связи стоит delete-orphan.
        session.delete(adset)
    session.flush()
    session.delete(account)
    return len(adsets)


def under_control_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(ControlledAdset.id)).where(
                ControlledAdset.state.in_([STATE_WATCHING, STATE_PAUSED])
            )
        )
        or 0
    )


def decisions_count(session: Session) -> int:
    return int(session.scalar(select(func.count(Decision.id))) or 0)
