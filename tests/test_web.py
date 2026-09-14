"""Вход и страницы — через настоящее приложение на временной базе."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, ROLE_ADMIN, ROLE_BUYER
from app.services import create_user, upsert_geo_threshold


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    import app.db as db
    import app.main as main

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", maker)

    def _get_session():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[db.get_session] = _get_session
    with maker() as s:
        create_user(s, "sasha", "secret1", role=ROLE_BUYER, display_name="SASHA")
        admin = create_user(s, "boss", "secret1", role=ROLE_ADMIN)
        upsert_geo_threshold(s, "IT", 11.0, 5.0, 0.2, user=admin)
        s.commit()

    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def login(client, user="sasha", password="secret1"):
    return client.post(
        "/login", data={"login": user, "password": password, "next": "/autocontrol"},
        follow_redirects=False,
    )


def test_anonymous_is_sent_to_login(client):
    response = client.get("/autocontrol", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Автоконтроль адсетов" in response.text


def test_successful_login_sets_cookie(client):
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/autocontrol"
    assert "ac_session" in response.cookies


def test_wrong_password_is_rejected(client):
    response = client.post(
        "/login", data={"login": "sasha", "password": "nope", "next": "/autocontrol"}
    )
    assert response.status_code == 401
    assert "Неверный логин или пароль" in response.text


def test_autocontrol_page_after_login(client):
    login(client)
    response = client.get("/autocontrol?tab=thresholds")
    assert response.status_code == 200
    assert "Общие пороги по гео" in response.text
    assert "Мои пороги" in response.text
    assert "IT" in response.text


def test_journal_tab_renders(client):
    login(client)
    response = client.get("/autocontrol?tab=journal")
    assert response.status_code == 200
    assert "Журнал решений" in response.text
    assert "включил обратно" in response.text


def test_buyer_cannot_edit_global_thresholds(client):
    login(client)
    response = client.post(
        "/thresholds/global",
        data={"geo": "ES", "max_cpa": "10", "max_spend_no_conv": "4", "max_ucpc": "0.17"},
    )
    assert response.status_code == 403


def test_admin_can_edit_global_thresholds(client):
    login(client, "boss")
    response = client.post(
        "/thresholds/global",
        data={"geo": "ES", "max_cpa": "10", "max_spend_no_conv": "4", "max_ucpc": "0.17"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/autocontrol?tab=thresholds")
    assert "ES" in page.text


def test_buyer_can_edit_own_thresholds(client):
    login(client)
    response = client.post(
        "/thresholds/mine",
        data={"geo": "it", "max_cpa": "6", "max_spend_no_conv": "3,5", "max_ucpc": "0.09"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/autocontrol?tab=thresholds")
    assert "0.0900" in page.text  # запятая в «3,5» принята, порог сохранён


def test_bad_geo_is_reported(client):
    login(client)
    response = client.post(
        "/thresholds/mine", data={"geo": "ITA", "max_cpa": "6"}
    )
    assert response.status_code == 400


def test_logout_clears_session(client):
    login(client)
    client.post("/logout", follow_redirects=False)
    response = client.get("/autocontrol", follow_redirects=False)
    assert response.status_code == 303


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_integrations_page_renders(client):
    """Страница содержит макрос {{adset.id}} как текст — он не должен исполняться."""
    login(client)
    response = client.get("/integrations")
    assert response.status_code == 200
    assert "Поле с ID адсета" in response.text
    assert "adset_id={{adset.id}}" in response.text
    assert "sub_id_6" in response.text


def test_keitaro_profile_can_be_edited(client):
    """Поле с ID адсета меняется в интерфейсе, ключ при этом не теряется."""
    from sqlalchemy import select

    import app.db as db
    from app.models import KeitaroProfile

    login(client)
    client.post(
        "/integrations/keitaro",
        data={
            "title": "Основной", "base_url": "https://kt.example.com", "api_key": "SECRET",
            "timezone_name": "Europe/Moscow", "adset_field": "sub_id_2",
        },
        follow_redirects=False,
    )
    with db.SessionLocal() as s:
        profile = s.scalar(select(KeitaroProfile))
        assert profile.adset_field == "sub_id_2"

    # Правим только поле, ключ оставляем пустым — он должен уцелеть.
    response = client.post(
        f"/integrations/keitaro/{profile.id}",
        data={
            "title": "Основной", "base_url": "https://kt.example.com", "api_key": "",
            "timezone_name": "Europe/Moscow", "adset_field": "sub_id_6",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.SessionLocal() as s:
        updated = s.scalar(select(KeitaroProfile))
        assert updated.adset_field == "sub_id_6"
        assert updated.api_key == "SECRET"
