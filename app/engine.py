"""Движок автоконтроля.

Один тик:
  1. адсеты группируются по кабинету — у каждого свой пояс и свой трекер;
  2. на группу строится окно суток от 00:00 по таймзоне кабинета;
  3. одним запросом в Keitaro берётся статистика за это окно;
  4. НАБЛЮДАЕТ  → превышен порог → выключаем адсет в Facebook;
     ВЫКЛЮЧИЛ   → метрики вернулись → включаем обратно;
                 → прошло 3 часа и не вернулись → снимаем контроль;
     ВЫКЛЮЧИЛ/СНЯТ → адсет снова активен в Facebook → «включил человек».

«Выключил» перепроверяется по дню остановки: долетевший лид меняет CPA именно
того дня, в котором адсет встал, даже если в поясе кабинета уже наступил новый.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.facebook import STATUS_ACTIVE, FacebookClient, FacebookError
from app.clients.keitaro import KeitaroClient, KeitaroError
from app.config import Settings, get_settings
from app.models import (
    DECISION_ERROR, DECISION_HUMAN_RESUMED, DECISION_PAUSED, DECISION_RELEASED,
    DECISION_RESUMED, STATE_PAUSED, STATE_RELEASED, STATE_WATCHING, AdAccount,
    ControlledAdset, Decision, utcnow,
)
from app.rules import RULE_TITLES, Breach, Metrics, evaluate
from app.tzwindow import DayWindow, UnknownTimezone, day_window

log = logging.getLogger(__name__)

UTC = timezone.utc

NOTE_RECOVERED = "метрики вернулись в норму"
NOTE_RELEASED = "за {hours} часов метрики не вернулись — контроль снят, адсет остаётся выключенным"
NOTE_HUMAN = "адсет включили вручную — автоконтроль продолжает следить"


@dataclass
class TickReport:
    """Итог одного тика — для логов и страницы состояния."""

    started_at: datetime = field(default_factory=utcnow)
    checked: int = 0
    paused: int = 0
    resumed: int = 0
    released: int = 0
    human_resumed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "checked": self.checked,
            "paused": self.paused,
            "resumed": self.resumed,
            "released": self.released,
            "human_resumed": self.human_resumed,
            "errors": self.errors,
        }


def _metrics_of(adset: ControlledAdset, metrics: Metrics) -> None:
    adset.last_spend = metrics.spend
    adset.last_conversions = metrics.conversions
    adset.last_leads = metrics.leads
    adset.last_unique_clicks = metrics.unique_clicks
    adset.last_checked_at = utcnow()


def record_decision(
    session: Session,
    adset: ControlledAdset,
    kind: str,
    *,
    metrics: Metrics | None = None,
    breach: Breach | None = None,
    rule: str | None = None,
    note: str = "",
    actor: str = "движок",
    window: DayWindow | None = None,
) -> Decision:
    """Пишет решение в журнал вместе с метриками на этот момент."""
    metrics = metrics or Metrics()
    decision = Decision(
        adset_pk=adset.id,
        adset_name=adset.name or adset.adset_id,
        kind=kind,
        rule=breach.rule if breach else rule,
        spend=metrics.spend,
        conversions=metrics.conversions,
        leads=metrics.leads,
        unique_clicks=metrics.unique_clicks,
        cpa=metrics.cpa,
        ucpc=metrics.ucpc,
        limit_value=breach.limit if breach else None,
        note=note,
        actor=actor,
        account_day=window.account_day if window else adset.control_day,
        window_from=window.start_utc if window else None,
        window_to=window.end_utc if window else None,
    )
    session.add(decision)
    return decision


class AutocontrolEngine:
    """Проход по всем адсетам под контролем."""

    def __init__(self, settings: Settings | None = None, now: datetime | None = None):
        self.settings = settings or get_settings()
        self._now_override = now

    # ------------------------------------------------------------------ время

    def now(self) -> datetime:
        return self._now_override or datetime.now(UTC)

    @property
    def recovery_window(self) -> timedelta:
        return timedelta(hours=self.settings.recovery_hours)

    # -------------------------------------------------------------- фабрики

    def keitaro_for(self, account: AdAccount) -> KeitaroClient:
        profile = account.keitaro
        if profile is None:
            raise KeitaroError(f"кабинету {account.title or account.account_id} не привязан Keitaro")
        return KeitaroClient(
            base_url=profile.base_url,
            api_key=profile.api_key,
            timezone_name=profile.timezone_name or self.settings.keitaro_timezone,
            adset_field=profile.adset_field or self.settings.keitaro_adset_field,
            timeout=self.settings.keitaro_timeout,
        )

    def facebook_for(self, account: AdAccount) -> FacebookClient:
        social = account.social
        if social is None or not social.access_token:
            raise FacebookError(f"кабинету {account.title or account.account_id} не привязан токен")
        return FacebookClient(
            access_token=social.access_token,
            api_version=self.settings.fb_api_version,
            timeout=self.settings.fb_timeout,
        )

    # ---------------------------------------------------------------- главный

    def tick(self, session: Session) -> TickReport:
        report = TickReport(started_at=self.now())

        adsets = session.scalars(
            select(ControlledAdset).where(
                ControlledAdset.state.in_([STATE_WATCHING, STATE_PAUSED, STATE_RELEASED])
            )
        ).all()

        by_account: dict[int, list[ControlledAdset]] = defaultdict(list)
        for adset in adsets:
            by_account[adset.account_pk].append(adset)

        for account_pk, group in by_account.items():
            account = session.get(AdAccount, account_pk)
            if account is None or not account.is_active:
                continue
            try:
                self._process_account(session, account, group, report)
            except (KeitaroError, FacebookError, UnknownTimezone) as exc:
                message = f"{account.title or account.account_id}: {exc}"
                log.warning("автоконтроль: %s", message)
                report.errors.append(message)
                self._mark_group_error(session, group, str(exc), report)

        session.commit()
        return report

    def _mark_group_error(
        self, session: Session, group: list[ControlledAdset], message: str, report: TickReport
    ) -> None:
        """Ошибка уровня кабинета — пишем её один раз на адсет, без дублей."""
        for adset in group:
            if adset.last_error == message:
                continue
            adset.last_error = message
            record_decision(
                session, adset, DECISION_ERROR, metrics=adset.last_metrics(), note=message
            )

    def _process_account(
        self,
        session: Session,
        account: AdAccount,
        group: list[ControlledAdset],
        report: TickReport,
    ) -> None:
        keitaro = self.keitaro_for(account)
        now = self.now()

        # Текущий день кабинета — для тех, кто наблюдает.
        today = day_window(account.timezone_name, now=now)

        # Выключенные проверяются по дню своей остановки.
        windows: dict[object, DayWindow] = {today.account_day: today}
        plan: dict[object, list[ControlledAdset]] = defaultdict(list)

        for adset in group:
            if adset.state == STATE_PAUSED and adset.control_day:
                key = adset.control_day
                if key not in windows:
                    windows[key] = day_window(
                        account.timezone_name, now=now, day=adset.control_day
                    )
            else:
                key = today.account_day
            plan[key].append(adset)

        # Один поход в Keitaro на каждое окно.
        for day_key, members in plan.items():
            window = windows[day_key]
            ids = [adset.adset_id for adset in members]
            metrics_by_id = keitaro.fetch_adset_metrics(window, ids)
            for adset in members:
                metrics = metrics_by_id.get(adset.adset_id, Metrics())
                self._apply(session, account, adset, metrics, window, report)

        if self.settings.human_resume_check:
            self._check_human_resume(session, account, group, report)

    # ------------------------------------------------------------- решения

    def _apply(
        self,
        session: Session,
        account: AdAccount,
        adset: ControlledAdset,
        metrics: Metrics,
        window: DayWindow,
        report: TickReport,
    ) -> None:
        report.checked += 1
        _metrics_of(adset, metrics)
        thresholds = adset.thresholds()

        if adset.state == STATE_WATCHING:
            breach = evaluate(metrics, thresholds)
            if breach is None:
                adset.last_error = ""
                return
            self._pause(session, account, adset, metrics, breach, window, report)
            return

        if adset.state == STATE_PAUSED:
            breach = evaluate(metrics, thresholds)
            if breach is None:
                self._resume(session, account, adset, metrics, window, report)
                return
            paused_at = _aware(adset.paused_at)
            if paused_at is None:
                # Данные разъехались: считаем отсчёт с этого тика, иначе адсет
                # остался бы выключенным навсегда и без решения в журнале.
                adset.paused_at = self.now()
                return
            if self.now() - paused_at >= self.recovery_window:
                self._release(session, adset, metrics, breach, window, report)

    def _pause(
        self,
        session: Session,
        account: AdAccount,
        adset: ControlledAdset,
        metrics: Metrics,
        breach: Breach,
        window: DayWindow,
        report: TickReport,
    ) -> None:
        try:
            if not self.settings.dry_run:
                self.facebook_for(account).pause_adset(adset.adset_id)
        except FacebookError as exc:
            adset.last_error = str(exc)
            record_decision(
                session, adset, DECISION_ERROR, metrics=metrics, breach=breach,
                note=f"не удалось выключить: {exc}", window=window,
            )
            report.errors.append(f"{adset.name or adset.adset_id}: {exc}")
            return

        adset.state = STATE_PAUSED
        adset.paused_at = self.now()
        adset.paused_rule = breach.rule
        adset.control_day = window.account_day
        adset.released_at = None
        adset.last_error = ""
        record_decision(session, adset, DECISION_PAUSED, metrics=metrics, breach=breach, window=window)
        report.paused += 1
        log.info(
            "выключил %s — %s %.4f > %.4f (день кабинета %s)",
            adset.name or adset.adset_id, RULE_TITLES[breach.rule], breach.value,
            breach.limit, window.account_day,
        )

    def _resume(
        self,
        session: Session,
        account: AdAccount,
        adset: ControlledAdset,
        metrics: Metrics,
        window: DayWindow,
        report: TickReport,
    ) -> None:
        try:
            if not self.settings.dry_run:
                self.facebook_for(account).resume_adset(adset.adset_id)
        except FacebookError as exc:
            adset.last_error = str(exc)
            record_decision(
                session, adset, DECISION_ERROR, metrics=metrics,
                note=f"не удалось включить обратно: {exc}", window=window,
            )
            report.errors.append(f"{adset.name or adset.adset_id}: {exc}")
            return

        rule = adset.paused_rule
        adset.state = STATE_WATCHING
        adset.paused_at = None
        adset.paused_rule = None
        adset.control_day = None
        adset.last_error = ""
        record_decision(
            session, adset, DECISION_RESUMED, metrics=metrics, rule=rule,
            note=NOTE_RECOVERED, window=window,
        )
        report.resumed += 1
        log.info("включил обратно %s — %s", adset.name or adset.adset_id, NOTE_RECOVERED)

    def _release(
        self,
        session: Session,
        adset: ControlledAdset,
        metrics: Metrics,
        breach: Breach,
        window: DayWindow,
        report: TickReport,
    ) -> None:
        """Три часа прошли, метрики не вернулись: адсет остаётся выключенным."""
        adset.state = STATE_RELEASED
        adset.released_at = self.now()
        record_decision(
            session, adset, DECISION_RELEASED, metrics=metrics, breach=breach,
            note=NOTE_RELEASED.format(hours=self.settings.recovery_hours), window=window,
        )
        report.released += 1
        log.info("снял контроль с %s", adset.name or adset.adset_id)

    def _check_human_resume(
        self,
        session: Session,
        account: AdAccount,
        group: list[ControlledAdset],
        report: TickReport,
    ) -> None:
        """Выключенный нами адсет снова активен — значит, его включил человек."""
        candidates = [a for a in group if a.state in {STATE_PAUSED, STATE_RELEASED}]
        if not candidates:
            return
        try:
            statuses = self.facebook_for(account).get_adsets_status(
                [a.adset_id for a in candidates]
            )
        except FacebookError as exc:
            log.debug("не удалось прочитать статусы адсетов: %s", exc)
            return

        for adset in candidates:
            info = statuses.get(adset.adset_id)
            if not info:
                continue
            effective = (info.get("effective_status") or info.get("status") or "").upper()
            if effective != STATUS_ACTIVE:
                continue
            adset.state = STATE_WATCHING
            adset.paused_at = None
            adset.paused_rule = None
            adset.control_day = None
            adset.released_at = None
            record_decision(
                session, adset, DECISION_HUMAN_RESUMED, metrics=adset.last_metrics(),
                note=NOTE_HUMAN, actor="человек",
            )
            report.human_resumed += 1


def _aware(moment: datetime | None) -> datetime | None:
    """SQLite отдаёт наивные datetime — считаем их UTC."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def run_once() -> TickReport:
    from app.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        return AutocontrolEngine().tick(session)


def run_forever() -> None:  # pragma: no cover - точка входа демона
    import time

    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    log.info(
        "движок автоконтроля запущен: тик %s с, окно возврата %s ч, Keitaro %s",
        settings.tick_seconds, settings.recovery_hours, settings.keitaro_timezone,
    )
    while True:
        try:
            report = run_once()
            log.info("тик: %s", report.as_dict())
        except Exception:
            log.exception("тик автоконтроля упал")
        time.sleep(settings.tick_seconds)


if __name__ == "__main__":  # pragma: no cover
    run_forever()
