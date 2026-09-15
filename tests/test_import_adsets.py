"""Диагностика import-adsets: почему найдено ноль."""
import argparse

import pytest
from sqlalchemy import select

import app.cli as cli
import app.clients.facebook as fb
import app.db as db
from app.models import Base, KeitaroProfile, SocialAccount, AdAccount, User
from app.services import create_user, upsert_geo_threshold


@pytest.fixture()
def wired(monkeypatch):
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
        admin = create_user(s, "admin", "secret1", role="admin")
        upsert_geo_threshold(s, "IT", 11, 5, 0.2, user=admin)
        k = KeitaroProfile(title="FB", base_url="https://kt", api_key="k", adset_field="sub_id_6")
        soc = SocialAccount(title="P")
        soc.access_token = "EAAB" + "x" * 40
        soc.cookies = "c_user=1; xs=2"
        s.add_all([k, soc])
        s.flush()
        s.add(AdAccount(account_id="777", title="Juneau", timezone_name="America/Juneau",
                        social_id=soc.id, keitaro_id=k.id))
        s.commit()
    return maker


def _run(capsys, **kw):
    base = {"account": "777", "geo": None, "user": None, "all": False, "dry_run": True}
    base.update(kw)
    cli.cmd_import_adsets(argparse.Namespace(**base))
    return capsys.readouterr().out


def test_reports_paused_only(wired, monkeypatch, capsys):
    def adsets(self, account_id, statuses=None):
        rows = [{"id": "1", "name": "eblo_it_x", "effective_status": "PAUSED",
                 "campaign": {"name": "[it] c"}}]
        return [] if statuses and "ACTIVE" in statuses else rows
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", adsets)
    out = _run(capsys)
    assert "активных нет" in out
    assert "PAUSED: 1" in out
    assert "--all" in out


def test_reports_empty_account(wired, monkeypatch, capsys):
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", lambda self, a, statuses=None: [])
    out = _run(capsys)
    assert "нет ни одного адсета" in out


def test_reports_geo_failure(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        fb.FacebookClient, "list_adsets",
        lambda self, a, statuses=None: [
            {"id": "3", "name": "noname", "effective_status": "ACTIVE", "campaign": {"name": "x"}}
        ],
    )
    out = _run(capsys)
    assert "гео не распозналось" in out


def test_geo_override_imports(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        fb.FacebookClient, "list_adsets",
        lambda self, a, statuses=None: [
            {"id": "3", "name": "noname", "effective_status": "ACTIVE", "campaign": {"name": "x"}}
        ],
    )
    out = _run(capsys, geo="it", dry_run=True)
    assert "поставил бы под контроль: 1" in out


def test_active_adsets_are_imported(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        fb.FacebookClient, "list_adsets",
        lambda self, a, statuses=None: [
            {"id": "9", "name": "eblo_it_z", "effective_status": "ACTIVE",
             "campaign": {"id": "c1", "name": "[AK47] [it] eblo"}}
        ],
    )
    _run(capsys, dry_run=False)
    with wired() as s:
        from app.models import ControlledAdset
        rows = s.scalars(select(ControlledAdset)).all()
    assert [r.adset_id for r in rows] == ["9"]
    assert rows[0].geo == "IT"


def test_suggests_geos_found_in_names(wired, monkeypatch, capsys):
    """Гео в именах есть, но не в порогах — команда их называет."""
    monkeypatch.setattr(
        fb.FacebookClient, "list_adsets",
        lambda self, a, statuses=None: [
            {"id": "1", "name": "zdrave_cz_klouby-D1", "effective_status": "ACTIVE",
             "campaign": {"name": "[AK47] [cz] zdrave"}},
            {"id": "2", "name": "t2_sk_joint-R2", "effective_status": "ACTIVE",
             "campaign": {"name": "[sk] joint"}},
        ],
    )
    out = _run(capsys)
    assert "нет в порогах: CZ" in out
    assert "SK" in out


def test_no_geo_tokens_shows_sample_names(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        fb.FacebookClient, "list_adsets",
        lambda self, a, statuses=None: [
            {"id": "1", "name": "promo_final_v2", "effective_status": "ACTIVE",
             "campaign": {"name": "summer sale"}},
        ],
    )
    out = _run(capsys)
    assert "нет кодов гео" in out
    assert "promo_final_v2" in out


def test_imports_across_all_accounts(wired, monkeypatch, capsys):
    """Без --account команда проходит по всем кабинетам."""
    from sqlalchemy import select

    import app.db as db
    from app.models import AdAccount, ControlledAdset, KeitaroProfile, SocialAccount

    # Заведём второй кабинет к тому, что создаёт wired.
    with wired() as s:
        k = s.scalar(select(KeitaroProfile))
        soc = s.scalar(select(SocialAccount))
        s.add(AdAccount(account_id="888", title="Второй", timezone_name="Europe/Warsaw",
                        social_id=soc.id, keitaro_id=k.id))
        s.commit()

    def adsets(self, account_id, statuses=None):
        return [{"id": f"{account_id}-1", "name": "cz1_it_x",
                 "effective_status": "ACTIVE", "campaign": {"name": "[it] c"}}]
    monkeypatch.setattr(fb.FacebookClient, "list_adsets", adsets)

    args = argparse.Namespace(account=None, geo=None, user=None, all=False, dry_run=False)
    cli.cmd_import_adsets(args)

    with wired() as s:
        rows = {a.adset_id for a in s.scalars(select(ControlledAdset))}
    # По одному адсету из каждого кабинета (777 из фикстуры + 888).
    assert rows == {"777-1", "888-1"}
    assert "по всем кабинетам: 2" in capsys.readouterr().out
