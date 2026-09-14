"""Разбор мультитокена и нормализация прокси."""
import pytest

from app.multitoken import (
    Multitoken, MultitokenError, normalize_cookies, normalize_proxy,
    parse_multitoken,
)

TOKEN = "EAAGm0PX4ZCpsB" + "A" * 40
COOKIES = "c_user=100012345678; xs=12%3Aabc%3A2%3A1600000000; datr=Zzz; fr=abc"


def test_token_and_cookies():
    mt = parse_multitoken(f"{TOKEN}|{COOKIES}")
    assert mt.access_token == TOKEN
    assert "c_user=100012345678" in mt.cookies
    assert mt.has_session
    # uid подтягивается из c_user, даже если отдельным куском его не было.
    assert mt.uid == "100012345678"


def test_full_form_with_password_and_agent():
    raw = f"100012345678|Passw0rd!|{TOKEN}|{COOKIES}|Mozilla/5.0 (Windows NT 10.0) Chrome/124"
    mt = parse_multitoken(raw)
    assert mt.access_token == TOKEN
    assert mt.uid == "100012345678"
    assert mt.user_agent.startswith("Mozilla/5.0")
    # Пароль не должен попасть ни в одно из полей.
    assert "Passw0rd!" not in mt.cookies
    assert "Passw0rd!" not in mt.access_token
    assert "Passw0rd!" in mt.unknown


def test_proxy_inside_multitoken():
    mt = parse_multitoken(f"{TOKEN}|{COOKIES}|1.2.3.4:8080:bob:secret")
    assert mt.proxy == "http://bob:secret@1.2.3.4:8080"


def test_json_form():
    raw = (
        '{"token":"%s","cookies":"c_user=555; xs=zz","ua":"Mozilla/5.0 X",'
        '"proxy":"socks5://u:p@10.0.0.1:1080"}' % TOKEN
    )
    mt = parse_multitoken(raw)
    assert mt.access_token == TOKEN
    assert mt.proxy == "socks5://u:p@10.0.0.1:1080"
    assert mt.user_agent == "Mozilla/5.0 X"


def test_cookies_as_browser_export():
    raw = '{"token":"%s","cookies":[{"name":"c_user","value":"777"},{"name":"xs","value":"qq"},{"name":"tracking","value":"drop"}]}' % TOKEN
    mt = parse_multitoken(raw)
    assert mt.cookies == "c_user=777; xs=qq"
    # Лишние куки не тащим — хранить чужой мусор незачем.
    assert "tracking" not in mt.cookies


def test_cookies_only_is_enough():
    """Без токена, но с сессией — токен потом достанем из кук."""
    mt = parse_multitoken(COOKIES)
    assert mt.access_token == ""
    assert mt.has_session


def test_token_only_is_enough():
    mt = parse_multitoken(TOKEN)
    assert mt.access_token == TOKEN
    assert not mt.has_session


def test_garbage_is_refused():
    with pytest.raises(MultitokenError, match="ни токена, ни кук"):
        parse_multitoken("просто какой-то текст")
    with pytest.raises(MultitokenError, match="пустой"):
        parse_multitoken("   ")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.2.3.4:8080", "http://1.2.3.4:8080"),
        ("1.2.3.4:8080:bob:sec", "http://bob:sec@1.2.3.4:8080"),
        ("bob:sec@1.2.3.4:8080", "http://bob:sec@1.2.3.4:8080"),
        ("socks5://u:p@h.example:1080", "socks5://u:p@h.example:1080"),
        ("http://1.2.3.4:3128", "http://1.2.3.4:3128"),
        ("SOCKS5H://1.2.3.4:1080", "socks5h://1.2.3.4:1080"),
    ],
)
def test_proxy_normalization(raw, expected):
    assert normalize_proxy(raw) == expected


def test_bad_proxy_is_refused():
    with pytest.raises(MultitokenError, match="формат прокси"):
        normalize_proxy("не-прокси-вовсе")


def test_describe_hides_secrets():
    mt = parse_multitoken(f"{TOKEN}|{COOKIES}|1.2.3.4:8080:bob:secret")
    text = mt.describe()
    assert TOKEN not in text
    assert "secret" not in text          # пароль прокси не светится
    assert "1.2.3.4:8080" in text        # а хост виден
    assert "c_user" in text


def test_cookie_string_keeps_only_session_cookies():
    assert normalize_cookies("c_user=1; xs=2; some_ad_tracker=3") == "c_user=1; xs=2"
