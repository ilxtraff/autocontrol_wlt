"""Работа от имени социального аккаунта: прокси, куки, перевыпуск токена."""
from datetime import date, datetime, timezone

import httpx
import pytest

from app.clients.facebook import FacebookClient, client_for_social
from app.config import get_settings
from app.crypto import decrypt
from app.engine import AutocontrolEngine
from app.models import AdAccount, KeitaroProfile, SocialAccount

UTC = timezone.utc
TOKEN_OLD = "EAAGold" + "A" * 40
TOKEN_NEW = "EAAGnew" + "B" * 50


def make_social(session, **kwargs):
    social = SocialAccount(title=kwargs.pop("title", "Камилла"))
    social.access_token = kwargs.pop("token", TOKEN_OLD)
    social.cookies = kwargs.pop("cookies", "c_user=100012345678; xs=secret")
    social.proxy = kwargs.pop("proxy", "http://bob:sec@1.2.3.4:8080")
    social.user_agent = kwargs.pop("user_agent", "Mozilla/5.0 Тест")
    for key, value in kwargs.items():
        setattr(social, key, value)
    session.add(social)
    session.flush()
    return social


def test_secrets_are_encrypted_at_rest(session):
    """В базе лежит шифртекст, а в коде — обычные строки."""
    social = make_social(session)
    session.commit()

    assert social.access_token == TOKEN_OLD
    assert social.access_token_enc.startswith("enc:v1:")
    assert TOKEN_OLD not in social.access_token_enc
    assert "secret" not in social.cookies_enc
    assert "sec@1.2.3.4" not in social.proxy_enc
    assert decrypt(social.proxy_enc) == "http://bob:sec@1.2.3.4:8080"


def test_proxy_label_hides_credentials(session):
    social = make_social(session)
    assert social.proxy_label == "http://1.2.3.4:8080"
    assert "bob" not in social.proxy_label
    assert "sec" not in social.proxy_label


def test_client_carries_proxy_cookies_and_agent(session):
    social = make_social(session)
    client = client_for_social(social, get_settings())

    kwargs = client._client_kwargs()
    assert kwargs["proxy"] == "http://bob:sec@1.2.3.4:8080"
    assert client._cookie_jar()["c_user"] == "100012345678"
    # Кириллица в user-agent сломала бы кодирование заголовков — берётся стандартный.
    assert kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0 (Windows")


def test_ascii_user_agent_is_kept(session):
    social = make_social(session, user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/124")
    kwargs = client_for_social(social, get_settings())._client_kwargs()
    assert kwargs["headers"]["User-Agent"] == "Mozilla/5.0 (Windows NT 10.0) Chrome/124"


def test_token_is_refreshed_from_cookies(session, monkeypatch):
    """Протухший токен поднимается из сессии без участия человека."""
    social = make_social(session, token_status="invalid")
    monkeypatch.setattr(
        FacebookClient, "refresh_token_from_cookies", lambda self: TOKEN_NEW
    )

    assert AutocontrolEngine().refresh_token(social) is True
    assert social.access_token == TOKEN_NEW
    assert social.token_status == "ok"
    assert social.token_error == ""
    assert social.token_refreshed_at is not None


def test_refresh_needs_session_cookies(session, monkeypatch):
    social = make_social(session, cookies="")
    monkeypatch.setattr(
        FacebookClient, "refresh_token_from_cookies", lambda self: TOKEN_NEW
    )
    assert AutocontrolEngine().refresh_token(social) is False
    assert social.access_token == TOKEN_OLD


def test_refresh_reports_failure_when_page_has_no_token(session, monkeypatch):
    social = make_social(session)
    monkeypatch.setattr(FacebookClient, "refresh_token_from_cookies", lambda self: "")
    assert AutocontrolEngine().refresh_token(social) is False


def test_refresh_extracts_token_from_page(monkeypatch):
    """Токен вытаскивается из разметки залогиненной страницы."""
    body = 'что-то ещё {"accessToken":"%s","other":1} хвост' % TOKEN_NEW

    def handler(request):
        assert request.headers["user-agent"] == "UA/test"
        assert "c_user=1" in request.headers.get("cookie", "")
        return httpx.Response(200, text=body)

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )
    client = FacebookClient("old", cookies="c_user=1; xs=2", user_agent="UA/test")
    assert client.refresh_token_from_cookies() == TOKEN_NEW


def test_engine_retries_the_call_after_refreshing(session, monkeypatch):
    """Выключение адсета не должно срываться из-за протухшего токена."""
    from app.clients.facebook import FacebookError

    keitaro = KeitaroProfile(title="kt", base_url="https://kt", api_key="k")
    session.add(keitaro)
    social = make_social(session)
    session.flush()
    account = AdAccount(
        account_id="1", timezone_name="America/Los_Angeles",
        social_id=social.id, keitaro_id=keitaro.id,
    )
    session.add(account)
    session.flush()

    calls = []

    def flaky(self, adset_id):
        calls.append(self.access_token)
        if self.access_token == TOKEN_OLD:
            raise FacebookError("Session expired", code=190)
        return True

    monkeypatch.setattr(FacebookClient, "pause_adset", flaky)
    monkeypatch.setattr(
        FacebookClient, "refresh_token_from_cookies", lambda self: TOKEN_NEW
    )

    engine = AutocontrolEngine()
    engine._with_token_retry(account, lambda fb: fb.pause_adset("1001"))

    assert calls == [TOKEN_OLD, TOKEN_NEW]
    assert social.access_token == TOKEN_NEW


def test_error_is_raised_when_refresh_also_fails(session, monkeypatch):
    from app.clients.facebook import FacebookError

    keitaro = KeitaroProfile(title="kt", base_url="https://kt", api_key="k")
    session.add(keitaro)
    social = make_social(session, cookies="")
    session.flush()
    account = AdAccount(account_id="1", timezone_name="UTC",
                        social_id=social.id, keitaro_id=keitaro.id)
    session.add(account)
    session.flush()

    def always_dead(self, adset_id):
        raise FacebookError("Session expired", code=190)

    monkeypatch.setattr(FacebookClient, "pause_adset", always_dead)
    with pytest.raises(FacebookError):
        AutocontrolEngine()._with_token_retry(account, lambda fb: fb.pause_adset("1"))
    assert social.token_status == "invalid"
