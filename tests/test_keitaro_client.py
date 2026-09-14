"""Клиент Keitaro: что именно уходит в запрос и как читается ответ."""
from datetime import date, datetime, timezone

import httpx
import pytest

from app.clients.keitaro import KeitaroClient, KeitaroError
from app.tzwindow import day_window

UTC = timezone.utc


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_request_carries_the_account_day_in_moscow_time():
    """00:00 кабинета в Лос-Анджелесе уходит в Keitaro как 10:00 по Москве."""
    captured = {}

    def handler(request):
        captured["body"] = request.read().decode()
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"rows": []})

    window = day_window("America/Los_Angeles", now=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    client = KeitaroClient(base_url="https://kt.example.com", api_key="KEY")
    client.fetch_adset_metrics(window, ["1001"], client=make_client(handler))

    import json

    body = json.loads(captured["body"])
    assert body["range"]["from"] == "2026-09-14 10:00:00"
    assert body["range"]["to"] == "2026-09-14 11:30:00"
    assert body["range"]["timezone"] == "Europe/Moscow"
    assert body["grouping"] == ["sub_id_2"]
    assert body["filters"][0] == {
        "name": "sub_id_2", "operator": "IN_LIST", "expression": ["1001"]
    }
    assert captured["headers"]["api-key"] == "KEY"
    assert captured["url"] == "https://kt.example.com/admin_api/v1/report/build"


def test_rows_are_parsed_into_metrics():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "sub_id_2": "1001", "cost": 6.32, "conversions": 0, "leads": 0,
                        "sales": 0, "clicks": 90, "campaign_unique_clicks": 77, "revenue": 0,
                    }
                ]
            },
        )

    window = day_window("America/Los_Angeles", now=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    client = KeitaroClient(base_url="https://kt", api_key="K")
    result = client.fetch_adset_metrics(window, ["1001", "1002"], client=make_client(handler))

    assert result["1001"].spend == 6.32
    assert result["1001"].unique_clicks == 77
    assert result["1001"].ucpc == pytest.approx(6.32 / 77)
    # Адсет без трафика Keitaro не возвращает — это нули, а не отсутствие данных.
    assert result["1002"].spend == 0.0
    assert result["1002"].cpa is None


def test_alternative_metric_names_are_tolerated():
    def handler(request):
        return httpx.Response(
            200, json={"rows": [{"sub_id_2": "1001", "cost": 3.0, "unique_clicks": 30}]}
        )

    window = day_window("GMT+0", now=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    client = KeitaroClient(base_url="https://kt", api_key="K")
    result = client.fetch_adset_metrics(window, ["1001"], client=make_client(handler))
    assert result["1001"].unique_clicks == 30


def test_custom_adset_field_is_used():
    captured = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"rows": []})

    window = day_window("GMT+0", now=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    client = KeitaroClient(base_url="https://kt", api_key="K", adset_field="sub_id_5")
    client.fetch_adset_metrics(window, ["1001"], client=make_client(handler))
    assert captured["body"]["grouping"] == ["sub_id_5"]


def test_http_error_is_wrapped():
    def handler(request):
        return httpx.Response(403, text="forbidden")

    window = day_window("GMT+0", now=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    client = KeitaroClient(base_url="https://kt", api_key="bad")
    with pytest.raises(KeitaroError, match="403"):
        client.fetch_adset_metrics(window, ["1001"], client=make_client(handler))


def test_empty_id_list_skips_the_request():
    def handler(request):  # pragma: no cover - не должен вызваться
        raise AssertionError("запрос не нужен")

    window = day_window("GMT+0", now=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    client = KeitaroClient(base_url="https://kt", api_key="K")
    assert client.fetch_adset_metrics(window, [], client=make_client(handler)) == {}
