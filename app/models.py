"""Схема данных автоконтроля."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.rules import Thresholds

UTC = timezone.utc


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
#  Роли и пользователи
# --------------------------------------------------------------------------- #

ROLE_BUYER = "buyer"
ROLE_ADMIN = "admin"
ROLE_CEO = "ceo"

ROLE_TITLES = {ROLE_BUYER: "buyer", ROLE_ADMIN: "admin", ROLE_CEO: "group CEO"}

# Общие пороги команды меняют только admin и group CEO — как в CRM.
ROLES_CAN_EDIT_GLOBAL = {ROLE_ADMIN, ROLE_CEO}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_BUYER)
    team: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def can_edit_global_thresholds(self) -> bool:
        return self.role in ROLES_CAN_EDIT_GLOBAL

    @property
    def role_title(self) -> str:
        return ROLE_TITLES.get(self.role, self.role)


# --------------------------------------------------------------------------- #
#  Интеграции
# --------------------------------------------------------------------------- #

class KeitaroProfile(Base):
    """Доступ к трекеру. Keitaro живёт по Москве — отсюда и пояс по умолчанию."""

    __tablename__ = "keitaro_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255))
    timezone_name: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    # Поле Keitaro, в котором лежит ID адсета. Номер зависит от того, на какой
    # sub_id в настройках трекера замаплен параметр adset_id из ссылки.
    adset_field: Mapped[str] = mapped_column(String(32), default="sub_id_6")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    accounts: Mapped[list["AdAccount"]] = relationship(back_populates="keitaro")


class SocialAccount(Base):
    """Социальный аккаунт Facebook целиком: токен, куки сессии и свой прокси.

    Запросы идут через прокси аккаунта — с чужого IP Facebook быстро выдаёт
    чекпоинт. Куки нужны не для самих вызовов Graph API (там хватает токена),
    а чтобы перевыпустить токен, когда он умрёт, без участия человека.

    Секреты лежат в базе зашифрованными: свойства ниже шифруют и расшифровывают
    их на лету, поэтому в коде с ними работают как с обычными строками.
    """

    __tablename__ = "social_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    fb_user_id: Mapped[str] = mapped_column(String(64), default="")

    # Ниже — зашифрованные значения; читать через одноимённые свойства без _enc.
    access_token_enc: Mapped[str] = mapped_column("access_token", Text, default="")
    cookies_enc: Mapped[str] = mapped_column("cookies", Text, default="")
    proxy_enc: Mapped[str] = mapped_column("proxy", Text, default="")

    user_agent: Mapped[str] = mapped_column(Text, default="")

    token_status: Mapped[str] = mapped_column(String(32), default="unknown")
    token_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    token_error: Mapped[str] = mapped_column(Text, default="")
    token_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    proxy_status: Mapped[str] = mapped_column(String(32), default="unknown")
    proxy_ip: Mapped[str] = mapped_column(String(64), default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    accounts: Mapped[list["AdAccount"]] = relationship(back_populates="social")

    # ------------------------------------------------------ секреты

    @property
    def access_token(self) -> str:
        from app.crypto import decrypt

        return decrypt(self.access_token_enc or "")

    @access_token.setter
    def access_token(self, value: str) -> None:
        from app.crypto import encrypt

        self.access_token_enc = encrypt(value or "")

    @property
    def cookies(self) -> str:
        from app.crypto import decrypt

        return decrypt(self.cookies_enc or "")

    @cookies.setter
    def cookies(self, value: str) -> None:
        from app.crypto import encrypt

        self.cookies_enc = encrypt(value or "")

    @property
    def proxy(self) -> str:
        from app.crypto import decrypt

        return decrypt(self.proxy_enc or "")

    @proxy.setter
    def proxy(self, value: str) -> None:
        from app.crypto import encrypt

        self.proxy_enc = encrypt(value or "")

    @property
    def has_session(self) -> bool:
        """Есть ли куки, по которым можно перевыпустить токен."""
        cookies = self.cookies
        return "c_user=" in cookies and "xs=" in cookies

    @property
    def token_tail(self) -> str:
        from app.crypto import mask

        return mask(self.access_token)

    @property
    def proxy_label(self) -> str:
        """Прокси без логина и пароля — для интерфейса."""
        from app.multitoken import _mask_proxy

        return _mask_proxy(self.proxy)


class AdAccount(Base):
    """Рекламный кабинет. Его таймзона задаёт начало суток для автоконтроля."""

    __tablename__ = "ad_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(191), default="")
    # Отдаётся Facebook как timezone_name (America/Los_Angeles); принимаем и «GMT-7».
    timezone_name: Mapped[str] = mapped_column(String(64), default="")
    currency: Mapped[str] = mapped_column(String(8), default="USD")

    social_id: Mapped[Optional[int]] = mapped_column(ForeignKey("social_accounts.id"))
    keitaro_id: Mapped[Optional[int]] = mapped_column(ForeignKey("keitaro_profiles.id"))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Когда движок последний раз искал в кабинете новые адсеты.
    adsets_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    social: Mapped[Optional[SocialAccount]] = relationship(back_populates="accounts")
    keitaro: Mapped[Optional[KeitaroProfile]] = relationship(back_populates="accounts")
    adsets: Mapped[list["ControlledAdset"]] = relationship(back_populates="account")


# --------------------------------------------------------------------------- #
#  Пороги
# --------------------------------------------------------------------------- #

class GeoThreshold(Base):
    """Общие пороги по гео — правила команды. Видят все, меняют admin и CEO."""

    __tablename__ = "geo_thresholds"
    __table_args__ = (UniqueConstraint("geo", name="uq_geo_threshold"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    geo: Mapped[str] = mapped_column(String(2), index=True)
    max_cpa: Mapped[Optional[float]] = mapped_column(Float)
    max_spend_no_conv: Mapped[Optional[float]] = mapped_column(Float)
    max_ucpc: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    updated_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))

    def as_thresholds(self) -> Thresholds:
        return Thresholds(self.max_cpa, self.max_spend_no_conv, self.max_ucpc)


class UserGeoThreshold(Base):
    """Мои пороги. Свой список, перебивает общий дефолт по этому гео."""

    __tablename__ = "user_geo_thresholds"
    __table_args__ = (UniqueConstraint("user_id", "geo", name="uq_user_geo_threshold"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    geo: Mapped[str] = mapped_column(String(2), index=True)
    max_cpa: Mapped[Optional[float]] = mapped_column(Float)
    max_spend_no_conv: Mapped[Optional[float]] = mapped_column(Float)
    max_ucpc: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def as_thresholds(self) -> Thresholds:
        return Thresholds(self.max_cpa, self.max_spend_no_conv, self.max_ucpc)


# --------------------------------------------------------------------------- #
#  Адсеты под контролем
# --------------------------------------------------------------------------- #

STATE_WATCHING = "watching"    # НАБЛЮДАЕТ
STATE_PAUSED = "paused"        # ВЫКЛЮЧИЛ — идёт трёхчасовое наблюдение
STATE_RELEASED = "released"    # СНЯТ — остаётся выключенным, включит только человек
STATE_OFF = "off"              # снят с автоконтроля вручную кнопкой «снять»

STATE_TITLES = {
    STATE_WATCHING: "НАБЛЮДАЕТ",
    STATE_PAUSED: "ВЫКЛЮЧИЛ",
    STATE_RELEASED: "СНЯТ",
    STATE_OFF: "БЕЗ КОНТРОЛЯ",
}

SOURCE_PERSONAL = "personal"
SOURCE_GLOBAL = "global"
SOURCE_MANUAL = "manual"

SOURCE_TITLES = {
    SOURCE_PERSONAL: "мой порог",
    SOURCE_GLOBAL: "общий по гео",
    SOURCE_MANUAL: "задан вручную",
}


class ControlledAdset(Base):
    """Адсет под автоконтролем.

    Пороги копируются сюда при заливе: правка справочника влияет на новые
    заливы, а идущие доживают по своим числам.
    """

    __tablename__ = "controlled_adsets"

    id: Mapped[int] = mapped_column(primary_key=True)
    adset_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(191), default="")
    campaign_id: Mapped[str] = mapped_column(String(64), default="")
    campaign_name: Mapped[str] = mapped_column(String(191), default="")
    geo: Mapped[str] = mapped_column(String(2), default="")

    account_pk: Mapped[int] = mapped_column(ForeignKey("ad_accounts.id"), index=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)

    # Снимок порогов на момент залива.
    max_cpa: Mapped[Optional[float]] = mapped_column(Float)
    max_spend_no_conv: Mapped[Optional[float]] = mapped_column(Float)
    max_ucpc: Mapped[Optional[float]] = mapped_column(Float)
    threshold_source: Mapped[str] = mapped_column(String(16), default=SOURCE_GLOBAL)

    state: Mapped[str] = mapped_column(String(16), default=STATE_WATCHING, index=True)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    paused_rule: Mapped[Optional[str]] = mapped_column(String(24))
    # День остановки в поясе кабинета: «выключил» перепроверяется именно по нему.
    control_day: Mapped[Optional[date]] = mapped_column(Date)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Последний известный срез — чтобы рисовать «сейчас / порог» без похода в API.
    last_spend: Mapped[float] = mapped_column(Float, default=0.0)
    last_conversions: Mapped[int] = mapped_column(Integer, default=0)
    last_leads: Mapped[int] = mapped_column(Integer, default=0)
    last_unique_clicks: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[AdAccount] = relationship(back_populates="adsets")
    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="adset", cascade="all, delete-orphan"
    )

    def thresholds(self) -> Thresholds:
        return Thresholds(self.max_cpa, self.max_spend_no_conv, self.max_ucpc)

    def last_metrics(self):
        from app.rules import Metrics

        return Metrics(
            spend=self.last_spend,
            conversions=self.last_conversions,
            leads=self.last_leads,
            unique_clicks=self.last_unique_clicks,
        )

    @property
    def state_title(self) -> str:
        return STATE_TITLES.get(self.state, self.state)

    @property
    def is_under_control(self) -> bool:
        return self.state in {STATE_WATCHING, STATE_PAUSED}


# --------------------------------------------------------------------------- #
#  Журнал решений
# --------------------------------------------------------------------------- #

DECISION_PAUSED = "paused"            # выключил
DECISION_RESUMED = "resumed"          # включил обратно
DECISION_RELEASED = "released"        # снял контроль
DECISION_HUMAN_RESUMED = "human"      # включил человек
DECISION_ERROR = "error"              # ошибка
DECISION_DETACHED = "detached"        # снят с автоконтроля

DECISION_TITLES = {
    DECISION_PAUSED: "выключил",
    DECISION_RESUMED: "включил обратно",
    DECISION_RELEASED: "снял контроль",
    DECISION_HUMAN_RESUMED: "включил человек",
    DECISION_ERROR: "ошибка",
    DECISION_DETACHED: "снял контроль вручную",
}


class Decision(Base):
    """Запись журнала — с метриками на момент решения."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    adset_pk: Mapped[int] = mapped_column(
        ForeignKey("controlled_adsets.id", ondelete="CASCADE"), index=True
    )
    adset_name: Mapped[str] = mapped_column(String(191), default="")
    kind: Mapped[str] = mapped_column(String(16), index=True)
    rule: Mapped[Optional[str]] = mapped_column(String(24), index=True)

    spend: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    leads: Mapped[int] = mapped_column(Integer, default=0)
    unique_clicks: Mapped[int] = mapped_column(Integer, default=0)
    cpa: Mapped[Optional[float]] = mapped_column(Float)
    ucpc: Mapped[Optional[float]] = mapped_column(Float)
    limit_value: Mapped[Optional[float]] = mapped_column(Float)

    note: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="движок")
    # Окно, по которому принято решение — день кабинета и его границы.
    account_day: Mapped[Optional[date]] = mapped_column(Date)
    window_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    window_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, server_default=func.now()
    )

    adset: Mapped[ControlledAdset] = relationship(back_populates="decisions")

    @property
    def kind_title(self) -> str:
        return DECISION_TITLES.get(self.kind, self.kind)
