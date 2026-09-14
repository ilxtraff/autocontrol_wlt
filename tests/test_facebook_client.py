"""Клиент Facebook: разбор ошибок и поиск кабинетов по токену профиля."""
import httpx
import pytest

from app.clients.facebook import FacebookClient, FacebookError


# Настоящий класс берём один раз: иначе повторная подмена в одном тесте
# обернула бы уже подменённый клиент, и транспорт первого перекрыл бы второй.
_REAL_CLIENT = httpx.Client


def patched(monkeypatch, routes):
    """Подменяет транспорт: routes — {путь: (код, тело)}."""
    def handler(request):
        path = request.url.path.split("/v21.0/")[-1]
        code, body = routes.get(
            path, (400, {"error": {"message": "Unsupported get request.", "code": 100}})
        )
        return httpx.Response(code, json=body)

    def fake(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*a, **kw)

    monkeypatch.setattr(httpx, "Client", fake)


def test_error_message_carries_the_code():
    """«Invalid request.» без кода не объясняет ничего."""
    exc = FacebookError("Invalid request.", code=100)
    assert str(exc) == "Invalid request. (#100)"
    assert "системного пользователя" in exc.hint


def test_token_problem_and_permission_problem_are_distinguished():
    assert FacebookError("x", code=190).is_token_problem
    assert not FacebookError("x", code=200).is_token_problem
    assert FacebookError("x", code=200).is_permission_problem


def test_profile_token_lists_accounts(monkeypatch):
    patched(monkeypatch, {
        "me/adaccounts": (200, {"data": [
            {"account_id": "111", "name": "Кабинет-1", "timezone_name": "America/Los_Angeles"},
        ]}),
    })
    accounts, notes = FacebookClient("tok").list_accounts_verbose()
    assert [a["account_id"] for a in accounts] == ["111"]
    assert notes == []


def test_system_user_token_is_named_in_the_notes(monkeypatch):
    """У системного пользователя нет me/adaccounts — это должно быть сказано вслух."""
    patched(monkeypatch, {
        "me/adaccounts": (400, {"error": {"message": "Invalid request.", "code": 100}}),
        "me/businesses": (200, {"data": []}),
    })
    accounts, notes = FacebookClient("tok").list_accounts_verbose()
    assert accounts == []
    assert any("#100" in n for n in notes)
    assert any("системного пользователя" in n for n in notes)


def test_falls_back_to_business_owned_accounts(monkeypatch):
    patched(monkeypatch, {
        "me/adaccounts": (400, {"error": {"message": "Invalid request.", "code": 100}}),
        "me/businesses": (200, {"data": [{"id": "biz1", "name": "БМ"}]}),
        "biz1/owned_ad_accounts": (200, {"data": [{"account_id": "222", "name": "К-2"}]}),
        "biz1/client_ad_accounts": (200, {"data": []}),
    })
    accounts, _ = FacebookClient("tok").list_accounts_verbose()
    assert [a["account_id"] for a in accounts] == ["222"]


def test_describe_token_tells_profile_from_system_user(monkeypatch):
    patched(monkeypatch, {
        "me": (200, {"id": "7", "name": "Камилла"}),
        "me/permissions": (200, {"data": [
            {"permission": "ads_management", "status": "granted"},
            {"permission": "ads_read", "status": "granted"},
            {"permission": "email", "status": "declined"},
        ]}),
    })
    info = FacebookClient("tok").describe_token()
    assert info["kind"] == "токен профиля"
    assert info["name"] == "Камилла"
    assert set(info["permissions"]) == {"ads_management", "ads_read"}

    # У системного пользователя ребра permissions нет.
    patched(monkeypatch, {"me": (200, {"id": "9", "name": "Система"})})
    info = FacebookClient("tok").describe_token()
    assert info["kind"] == "токен системного пользователя"


def test_paging_is_followed(monkeypatch):
    pages = [
        (200, {"data": [{"account_id": "1"}],
               "paging": {"next": "u", "cursors": {"after": "A"}}}),
        (200, {"data": [{"account_id": "2"}], "paging": {}}),
    ]

    def handler(request):
        code, body = pages.pop(0) if pages else (200, {"data": []})
        return httpx.Response(code, json=body)

    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: _REAL_CLIENT(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )
    accounts, _ = FacebookClient("tok").list_accounts_verbose()
    assert {a["account_id"] for a in accounts} == {"1", "2"}


def test_network_failure_is_not_blamed_on_the_token(monkeypatch):
    """Закрытый фаервол не должен выглядеть как протухший токен."""
    def handler(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: _REAL_CLIENT(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )
    info = FacebookClient("tok").describe_token()
    assert "недоступен" in info["error"]
    assert "сеть сервера" in info["hint"]

    exc = FacebookError("Facebook недоступен: boom", kind="transport")
    assert exc.is_transport_problem
    assert not exc.is_token_problem
