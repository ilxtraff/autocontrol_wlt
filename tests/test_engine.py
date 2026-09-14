"""Поведение движка: выключение, возврат в норму и снятие контроля."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.engine import AutocontrolEngine
from app.models import (
    DECISION_ERROR, DECISION_HUMAN_RESUMED, DECISION_PAUSED, DECISION_RELEASED,
    DECISION_RESUMED, STATE_PAUSED, STATE_RELEASED, STATE_WATCHING, AdAccount,
    ControlledAdset, Decision, KeitaroProfile, SocialAccount,
)
from app.rules import RULE_SPEND_NO_CONV, RULE_UCPC, Metrics

UTC = timezone.utc
T0 = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)  # 03:00 в Лос-Анджелесе


class FakeFacebook:
    def __init__(self, statuses=None, fail=False):
        self.calls = []
        self.statuses = statuses or {}
        self.fail = fail

    def pause_adset(self, adset_id):
        if self.fail:
            from app.clients.facebook import FacebookError

            raise FacebookError("токен протух", code=190)
        self.calls.append(("pause", adset_id))
        return True

    def resume_adset(self, adset_id):
        self.calls.append(("resume", adset_id))
        return True

    def get_adsets_status(self, ids):
        return {i: {"id": i, "effective_status": self.statuses.get(i, "PAUSED")} for i in ids}


class FakeKeitaro:
    """Отдаёт заранее заданные метрики и запоминает, за какие окна их спросили."""

    def __init__(self, by_day):
        self.by_day = by_day
        self.windows = []

    def fetch_adset_metrics(self, window, adset_ids, client=None):
        self.windows.append(window)
        data = self.by_day.get(window.account_day, {})
        return {i: data.get(i, Metrics()) for i in adset_ids}


class Harness(AutocontrolEngine):
    def __init__(self, keitaro, facebook, now):
        super().__init__(settings=get_settings(), now=now)
        self._keitaro = keitaro
        self._facebook = facebook

    def keitaro_for(self, account):
        return self._keitaro

    def facebook_for(self, account):
        return self._facebook


@pytest.fixture()
def account(session):
    keitaro = KeitaroProfile(title="kt", base_url="https://kt", api_key="k")
    social = SocialAccount(title="fb", access_token="tok")
    session.add_all([keitaro, social])
    session.flush()
    acc = AdAccount(
        account_id="999", title="乐启智抖-2", timezone_name="America/Los_Angeles",
        social_id=social.id, keitaro_id=keitaro.id,
    )
    session.add(acc)
    session.flush()
    return acc


def make_adset(session, account, **kwargs):
    adset = ControlledAdset(
        adset_id=kwargs.pop("adset_id", "1001"),
        name=kwargs.pop("name", "eblo2_it_ero-B4"),
        geo="IT", account_pk=account.id,
        max_cpa=11.00, max_spend_no_conv=5.00, max_ucpc=0.2000,
        state=kwargs.pop("state", STATE_WATCHING), **kwargs,
    )
    session.add(adset)
    session.flush()
    return adset


def decisions(session, adset):
    return (
        session.query(Decision)
        .filter(Decision.adset_pk == adset.id)
        .order_by(Decision.id)
        .all()
    )


# --------------------------------------------------------------------- выключение

def test_pauses_adset_over_threshold(session, account):
    adset = make_adset(session, account)
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=1.42, unique_clicks=7)}})
    facebook = FakeFacebook()

    report = Harness(keitaro, facebook, T0).tick(session)

    assert report.paused == 1
    assert facebook.calls == [("pause", "1001")]
    assert adset.state == STATE_PAUSED
    assert adset.paused_rule == RULE_UCPC
    assert adset.control_day == date(2026, 9, 14)
    log = decisions(session, adset)
    assert [d.kind for d in log] == [DECISION_PAUSED]
    assert log[0].rule == RULE_UCPC
    assert log[0].ucpc == pytest.approx(0.2029, abs=1e-4)
    assert log[0].limit_value == 0.2000


def test_leaves_healthy_adset_alone(session, account):
    adset = make_adset(session, account)
    keitaro = FakeKeitaro(
        {date(2026, 9, 14): {"1001": Metrics(spend=8.08, conversions=2, leads=2, unique_clicks=96)}}
    )
    facebook = FakeFacebook()

    report = Harness(keitaro, facebook, T0).tick(session)

    assert report.paused == 0
    assert facebook.calls == []
    assert adset.state == STATE_WATCHING
    assert adset.last_spend == 8.08


def test_day_window_uses_account_timezone(session, account):
    """Запрос в Keitaro уходит за сутки кабинета, а не за московские."""
    make_adset(session, account)
    keitaro = FakeKeitaro({})
    Harness(keitaro, FakeFacebook(), T0).tick(session)

    window = keitaro.windows[0]
    assert window.start_utc == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)  # 00:00 в LA
    assert window.account_day == date(2026, 9, 14)


# ----------------------------------------------------------------- возврат в норму

def test_resumes_when_metrics_recover(session, account):
    """Метрики вернулись в норму до истечения трёх часов — включаем обратно."""
    adset = make_adset(
        session, account, state=STATE_PAUSED, paused_at=T0 - timedelta(hours=1),
        paused_rule=RULE_UCPC, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=1.40, unique_clicks=7)}})
    facebook = FakeFacebook()

    report = Harness(keitaro, facebook, T0).tick(session)

    assert report.resumed == 1
    assert facebook.calls == [("resume", "1001")]
    assert adset.state == STATE_WATCHING
    assert adset.paused_at is None and adset.control_day is None
    last = decisions(session, adset)[-1]
    assert last.kind == DECISION_RESUMED
    assert last.note == "метрики вернулись в норму"
    assert last.rule == RULE_UCPC


def test_recheck_uses_the_day_the_adset_was_stopped(session, account):
    """Остановлен вчера по календарю кабинета — перепроверяем вчерашний день."""
    adset = make_adset(
        session, account, state=STATE_PAUSED, paused_at=T0 - timedelta(hours=1),
        paused_rule=RULE_SPEND_NO_CONV, control_day=date(2026, 9, 13),
    )
    keitaro = FakeKeitaro(
        {
            date(2026, 9, 13): {"1001": Metrics(spend=6.00, conversions=1, leads=1, unique_clicks=80)},
            date(2026, 9, 14): {"1001": Metrics(spend=99.0, unique_clicks=1)},
        }
    )
    report = Harness(keitaro, FakeFacebook(), T0).tick(session)

    # Долетевший лид сделал CPA $6 при пороге $11 — адсет возвращается в работу.
    assert report.resumed == 1
    assert adset.state == STATE_WATCHING
    assert {w.account_day for w in keitaro.windows} == {date(2026, 9, 13)}


def test_late_lead_revives_adset_stopped_on_spend(session, account):
    adset = make_adset(
        session, account, state=STATE_PAUSED, paused_at=T0 - timedelta(minutes=30),
        paused_rule=RULE_SPEND_NO_CONV, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro(
        {date(2026, 9, 14): {"1001": Metrics(spend=6.32, conversions=1, leads=1, unique_clicks=77)}}
    )
    Harness(keitaro, FakeFacebook(), T0).tick(session)
    assert adset.state == STATE_WATCHING


# ------------------------------------------------------------------ снятие контроля

def test_releases_after_three_hours(session, account):
    """Три часа прошли, метрики не вернулись — контроль снят, адсет остаётся выключен."""
    adset = make_adset(
        session, account, state=STATE_PAUSED, paused_at=T0 - timedelta(hours=3, minutes=1),
        paused_rule=RULE_SPEND_NO_CONV, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=5.13, unique_clicks=38)}})
    facebook = FakeFacebook()

    report = Harness(keitaro, facebook, T0).tick(session)

    assert report.released == 1
    assert adset.state == STATE_RELEASED
    assert ("resume", "1001") not in facebook.calls  # остаётся выключенным
    last = decisions(session, adset)[-1]
    assert last.kind == DECISION_RELEASED
    assert last.note == "за 3 часов метрики не вернулись — контроль снят, адсет остаётся выключенным"


def test_keeps_watching_inside_the_three_hour_window(session, account):
    adset = make_adset(
        session, account, state=STATE_PAUSED, paused_at=T0 - timedelta(hours=2, minutes=59),
        paused_rule=RULE_SPEND_NO_CONV, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=5.13, unique_clicks=38)}})
    report = Harness(keitaro, FakeFacebook(), T0).tick(session)

    assert report.released == 0
    assert adset.state == STATE_PAUSED


def test_released_adset_is_not_touched_again(session, account):
    adset = make_adset(
        session, account, state=STATE_RELEASED, paused_at=T0 - timedelta(hours=9),
        paused_rule=RULE_UCPC, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=50.0, unique_clicks=1)}})
    facebook = FakeFacebook()
    Harness(keitaro, facebook, T0).tick(session)

    assert adset.state == STATE_RELEASED
    assert facebook.calls == []


# ------------------------------------------------------------------ включил человек

def test_detects_manual_resume(session, account):
    adset = make_adset(
        session, account, state=STATE_RELEASED, paused_at=T0 - timedelta(hours=5),
        paused_rule=RULE_UCPC, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=50.0, unique_clicks=1)}})
    facebook = FakeFacebook(statuses={"1001": "ACTIVE"})

    report = Harness(keitaro, facebook, T0).tick(session)

    assert report.human_resumed == 1
    assert adset.state == STATE_WATCHING
    assert decisions(session, adset)[-1].kind == DECISION_HUMAN_RESUMED


# ------------------------------------------------------------------------- ошибки

def test_facebook_failure_is_logged_and_adset_stays_watching(session, account):
    adset = make_adset(session, account)
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=9.0, unique_clicks=10)}})
    facebook = FakeFacebook(fail=True)

    report = Harness(keitaro, facebook, T0).tick(session)

    assert report.paused == 0
    assert adset.state == STATE_WATCHING  # не считаем выключённым то, что не выключилось
    assert "токен протух" in adset.last_error
    assert decisions(session, adset)[-1].kind == DECISION_ERROR


def test_metrics_snapshot_is_stored_on_every_tick(session, account):
    adset = make_adset(session, account)
    keitaro = FakeKeitaro(
        {date(2026, 9, 14): {"1001": Metrics(spend=2.5, conversions=1, leads=1, unique_clicks=40)}}
    )
    Harness(keitaro, FakeFacebook(), T0).tick(session)

    assert adset.last_spend == 2.5
    assert adset.last_leads == 1
    assert adset.last_unique_clicks == 40
    assert adset.last_checked_at is not None


def test_paused_without_timestamp_gets_one(session, account):
    """Разъехавшиеся данные не должны оставлять адсет выключенным навсегда."""
    adset = make_adset(
        session, account, state=STATE_PAUSED, paused_at=None,
        paused_rule=RULE_UCPC, control_day=date(2026, 9, 14),
    )
    keitaro = FakeKeitaro({date(2026, 9, 14): {"1001": Metrics(spend=9.0, unique_clicks=10)}})
    Harness(keitaro, FakeFacebook(), T0).tick(session)

    assert adset.state == STATE_PAUSED
    assert adset.paused_at == T0  # отсчёт трёх часов начался с этого тика
