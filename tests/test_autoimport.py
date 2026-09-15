"""Автоимпорт адсетов движком при тике."""
from datetime import timedelta

import pytest
from sqlalchemy import select

import app.clients.facebook as fb
import app.clients.keitaro as kt
from app.config import get_settings
from app.engine import AutocontrolEngine
from app.models import (
    STATE_OFF, STATE_WATCHING, AdAccount, ControlledAdset, KeitaroProfile,
    SocialAccount, User,
)
from app.services import create_user, upsert_geo_threshold


@pytest.fixture()
def account(session, monkeypatch):
    admin = create_user(session, "admin", "secret1", role="admin")
    upsert_geo_threshold(session, "CZ", 11, 5, 0.2, user=admin)
    k = KeitaroProfile(title="FB", base_url="https://kt", api_key="k", adset_field="sub_id_6")
    soc = SocialAccount(title="P")
    soc.access_token = "EAAB" + "x" * 40
    soc.cookies = "c_user=1; xs=2"
    session.add_all([k, soc])
    session.flush()
    acc = AdAccount(account_id="111", title="Новый РК", timezone_name="America/Juneau",
                    social_id=soc.id, keitaro_id=k.id)
    session.add(acc)
    session.flush()
    # Keitaro без статистики — автоимпорту она не нужна.
    monkeypatch.setattr(kt.KeitaroClient, "fetch_adset_metrics",
                        lambda self, w, ids, client=None: {})
    return acc


def _adsets(rows):
    return lambda self, a, statuses=None: rows


def test_new_adsets_are_imported_on_tick(session, account, monkeypatch):
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", _adsets([
        {"id": "cz1", "name": "cz111", "effective_status": "ACTIVE", "campaign": {"name": "cz219"}},
        {"id": "cz2", "name": "cz112", "effective_status": "ACTIVE", "campaign": {"name": "cz220"}},
    ]))
    report = AutocontrolEngine().tick(session)
    assert report.imported == 2
    rows = {a.adset_id: a for a in session.scalars(select(ControlledAdset))}
    assert set(rows) == {"cz1", "cz2"}
    assert rows["cz1"].geo == "CZ"
    assert rows["cz1"].state == STATE_WATCHING


def test_second_tick_is_throttled(session, account, monkeypatch):
    calls = []

    def counting(self, a, statuses=None):
        calls.append(a)
        return []
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", counting)

    engine = AutocontrolEngine()
    engine.tick(session)
    engine.tick(session)  # сразу второй — не должен снова лезть в FB
    assert len(calls) == 1


def test_detached_adset_is_not_resurrected(session, account, monkeypatch):
    # Адсет уже снят вручную.
    session.add(ControlledAdset(
        adset_id="cz1", name="cz111", geo="CZ", account_pk=account.id,
        max_cpa=11, max_spend_no_conv=5, max_ucpc=0.2, state=STATE_OFF,
    ))
    session.flush()
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", _adsets([
        {"id": "cz1", "name": "cz111", "effective_status": "ACTIVE", "campaign": {"name": "cz219"}},
    ]))
    report = AutocontrolEngine().tick(session)
    assert report.imported == 0
    row = session.scalar(select(ControlledAdset).where(ControlledAdset.adset_id == "cz1"))
    assert row.state == STATE_OFF  # остался снятым


def test_unknown_geo_is_skipped(session, account, monkeypatch):
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", _adsets([
        {"id": "de1", "name": "de999", "effective_status": "ACTIVE", "campaign": {"name": "de111"}},
    ]))
    report = AutocontrolEngine().tick(session)
    assert report.imported == 0  # DE нет в порогах


def test_disabled_by_setting(session, account, monkeypatch):
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", _adsets([
        {"id": "cz1", "name": "cz111", "effective_status": "ACTIVE", "campaign": {"name": "cz219"}},
    ]))
    settings = get_settings()
    object.__setattr__(settings, "auto_import_adsets", False)
    try:
        report = AutocontrolEngine(settings=settings).tick(session)
        assert report.imported == 0
        assert session.scalars(select(ControlledAdset)).all() == []
    finally:
        object.__setattr__(settings, "auto_import_adsets", True)
