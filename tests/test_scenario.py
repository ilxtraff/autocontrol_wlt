"""Сквозной сценарий суток кабинета в Лос-Анджелесе.

Проверяем связку целиком: день считается по поясу кабинета, статистика берётся
из Keitaro по московскому времени, адсет выключается, возвращается в работу и в
итоге снимается с контроля.
"""
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.engine import AutocontrolEngine
from app.models import (
    DECISION_PAUSED, DECISION_RELEASED, DECISION_RESUMED, STATE_PAUSED,
    STATE_RELEASED, STATE_WATCHING, AdAccount, ControlledAdset, Decision,
    KeitaroProfile, SocialAccount,
)
from app.rules import RULE_SPEND_NO_CONV, RULE_UCPC, Metrics
from app.tzwindow import to_tracker_range

UTC = timezone.utc


class ScriptedKeitaro:
    """Отдаёт метрики по расписанию и запоминает московские границы запросов."""

    def __init__(self, script):
        self.script = script
        self.asked = []

    def fetch_adset_metrics(self, window, adset_ids, client=None):
        self.asked.append(to_tracker_range(window, "Europe/Moscow"))
        return {i: self.script.get(self.now_key, Metrics()) for i in adset_ids}


class RecordingFacebook:
    def __init__(self):
        self.calls = []

    def pause_adset(self, adset_id):
        self.calls.append(("pause", adset_id))
        return True

    def resume_adset(self, adset_id):
        self.calls.append(("resume", adset_id))
        return True

    def get_adsets_status(self, ids):
        return {i: {"id": i, "effective_status": "PAUSED"} for i in ids}


class Harness(AutocontrolEngine):
    def __init__(self, keitaro, facebook, now):
        super().__init__(settings=get_settings(), now=now)
        self._k, self._f = keitaro, facebook

    def keitaro_for(self, account):
        return self._k

    def facebook_for(self, account):
        return self._f


def test_full_day_lifecycle(session):
    keitaro_profile = KeitaroProfile(title="kt", base_url="https://kt", api_key="k")
    social = SocialAccount(title="fb", access_token="tok")
    session.add_all([keitaro_profile, social])
    session.flush()
    account = AdAccount(
        account_id="1790847419029975", title="乐启智抖-2 (10:00)",
        timezone_name="America/Los_Angeles", social_id=social.id,
        keitaro_id=keitaro_profile.id,
    )
    session.add(account)
    session.flush()

    adset = ControlledAdset(
        adset_id="1001", name="eblo2_it_ero-B4", geo="IT", account_pk=account.id,
        max_cpa=11.00, max_spend_no_conv=5.00, max_ucpc=0.2000, state=STATE_WATCHING,
    )
    session.add(adset)
    session.flush()

    keitaro = ScriptedKeitaro({})
    facebook = RecordingFacebook()

    def tick(at, metrics):
        keitaro.script = {"now": metrics}
        keitaro.now_key = "now"
        return Harness(keitaro, facebook, at).tick(session)

    # 01:00 по кабинету — трафик в норме.
    tick(datetime(2026, 9, 14, 8, 0, tzinfo=UTC), Metrics(spend=0.80, unique_clicks=8))
    assert adset.state == STATE_WATCHING

    # Первый запрос ушёл в Keitaro за сутки кабинета в московском времени.
    assert keitaro.asked[0][0] == "2026-09-14 10:00:00"

    # 03:29 — uCPC пробил порог, адсет выключается.
    t_pause = datetime(2026, 9, 14, 10, 29, tzinfo=UTC)
    tick(t_pause, Metrics(spend=1.42, unique_clicks=7))
    assert adset.state == STATE_PAUSED
    assert adset.paused_rule == RULE_UCPC
    assert adset.control_day == date(2026, 9, 14)
    assert ("pause", "1001") in facebook.calls

    # Через час метрики вернулись в норму — адсет включается обратно.
    tick(t_pause + timedelta(minutes=69), Metrics(spend=1.40, unique_clicks=7))
    assert adset.state == STATE_WATCHING
    assert ("resume", "1001") in facebook.calls

    # Позже расход без конверсий перевалил за $5 — снова выключаем.
    t_pause2 = t_pause + timedelta(hours=4)
    tick(t_pause2, Metrics(spend=5.04, unique_clicks=37))
    assert adset.state == STATE_PAUSED
    assert adset.paused_rule == RULE_SPEND_NO_CONV

    # Два часа спустя ничего не изменилось — контроль ещё держится.
    tick(t_pause2 + timedelta(hours=2), Metrics(spend=5.13, unique_clicks=38))
    assert adset.state == STATE_PAUSED

    # После трёх часов контроль снимается, адсет остаётся выключенным.
    tick(t_pause2 + timedelta(hours=3, minutes=9), Metrics(spend=5.13, unique_clicks=38))
    assert adset.state == STATE_RELEASED
    assert facebook.calls.count(("resume", "1001")) == 1  # обратно не включали

    journal = session.query(Decision).order_by(Decision.id).all()
    assert [d.kind for d in journal] == [
        DECISION_PAUSED, DECISION_RESUMED, DECISION_PAUSED, DECISION_RELEASED
    ]
    assert journal[1].note == "метрики вернулись в норму"
    assert journal[3].note.startswith("за 3 часов метрики не вернулись")
    # Каждое решение несёт метрики того момента.
    assert journal[0].ucpc is not None and journal[0].spend == 1.42


def test_new_account_day_starts_at_local_midnight(session):
    """В 23:59 и в 00:01 по кабинету запрашиваются разные сутки."""
    keitaro_profile = KeitaroProfile(title="kt", base_url="https://kt", api_key="k")
    social = SocialAccount(title="fb", access_token="tok")
    session.add_all([keitaro_profile, social])
    session.flush()
    account = AdAccount(
        account_id="1", timezone_name="America/Los_Angeles",
        social_id=social.id, keitaro_id=keitaro_profile.id,
    )
    session.add(account)
    session.flush()
    session.add(
        ControlledAdset(
            adset_id="1", name="a", geo="IT", account_pk=account.id,
            max_cpa=11.0, max_spend_no_conv=5.0, max_ucpc=0.2, state=STATE_WATCHING,
        )
    )
    session.flush()

    keitaro = ScriptedKeitaro({})
    keitaro.now_key = "x"
    facebook = RecordingFacebook()

    # 23:59 в Лос-Анджелесе 14-го = 06:59 UTC 15-го.
    Harness(keitaro, facebook, datetime(2026, 9, 15, 6, 59, tzinfo=UTC)).tick(session)
    # 00:01 в Лос-Анджелесе 15-го = 07:01 UTC 15-го.
    Harness(keitaro, facebook, datetime(2026, 9, 15, 7, 1, tzinfo=UTC)).tick(session)

    assert keitaro.asked[0][0] == "2026-09-14 10:00:00"  # сутки 14-го
    assert keitaro.asked[1][0] == "2026-09-15 10:00:00"  # уже сутки 15-го
