"""Массовый импорт кабинетов из токена."""
import argparse

import pytest
from sqlalchemy import select

import app.cli as cli
import app.clients.facebook as fb
import app.db as db
from app.models import AdAccount, Base, KeitaroProfile, SocialAccount


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Настоящее приложение на временной базе + фейковый Facebook."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", maker)

    with maker() as s:
        s.add(KeitaroProfile(title="FB", base_url="https://kt", api_key="k", adset_field="sub_id_6"))
        soc = SocialAccount(title="Patrcyja")
        soc.access_token = "EAAB" + "x" * 40
        soc.cookies = "c_user=1; xs=2"
        s.add(soc)
        s.commit()

    accounts = [
        {"account_id": "111", "name": "К-1", "timezone_name": "America/Los_Angeles", "currency": "USD"},
        {"id": "act_222", "name": "К-2", "timezone_name": "Europe/Warsaw", "currency": "PLN"},
        {"account_id": "333", "name": "К-3", "timezone_name": "", "currency": "USD"},
    ]
    monkeypatch.setattr(fb.FacebookClient, "list_accounts", lambda self: accounts)
    return maker


def _run(**kw):
    base = {"social": None, "keitaro": None, "filter": None, "quiet": True}
    base.update(kw)
    cli.cmd_import_accounts(argparse.Namespace(**base))


def test_accounts_are_registered(wired):
    _run()
    with wired() as s:
        rows = {a.account_id: a for a in s.scalars(select(AdAccount))}
    assert set(rows) == {"111", "222", "333"}
    assert rows["111"].timezone_name == "America/Los_Angeles"
    assert rows["222"].account_id == "222"  # act_ снят
    assert rows["222"].currency == "PLN"
    # Все привязаны к аккаунту и трекеру.
    assert all(a.social_id and a.keitaro_id for a in rows.values())


def test_unknown_timezone_left_blank(wired, monkeypatch):
    accounts = [{"account_id": "999", "name": "X", "timezone_name": "Мордор/Тьма", "currency": "USD"}]
    monkeypatch.setattr(fb.FacebookClient, "list_accounts", lambda self: accounts)
    _run()
    with wired() as s:
        acc = s.scalar(select(AdAccount).where(AdAccount.account_id == "999"))
    assert acc.timezone_name == ""  # мусорную таймзону не сохранили


def test_rerun_is_idempotent(wired):
    _run()
    _run()
    with wired() as s:
        assert len(s.scalars(select(AdAccount)).all()) == 3  # не задвоились


def test_filter_limits_import(wired):
    _run(filter="к-2")
    with wired() as s:
        ids = [a.account_id for a in s.scalars(select(AdAccount))]
    assert ids == ["222"]


def test_missing_keitaro_is_reported(wired, monkeypatch):
    with wired() as s:
        for k in s.scalars(select(KeitaroProfile)):
            s.delete(k)
        s.commit()
    with pytest.raises(SystemExit, match="трекер"):
        _run()
