"""Гео из названий кампаний и адсетов — разные написания."""
import pytest

from app.naming import detect_geo, geo_candidates

KNOWN = {"CZ", "SK", "PL", "IT", "RO"}


@pytest.mark.parametrize(
    "campaign, expected",
    [
        ("[CZ] Joints AK47", "CZ"),        # код в скобках
        ("[cz] joints", "CZ"),             # строчными
        ("CZ | Joints | AK47", "CZ"),      # код без скобок, через |
        ("cz_joints_broad", "CZ"),         # через подчёркивание
        ("Czech Republic - Joints", "CZ"), # полное название
        ("Poland Joints Broad", "PL"),
        ("PL / test", "PL"),
        ("WW Broad Joints", None),         # ww — не гео
        ("summer sale", None),             # гео нет
    ],
)
def test_detect_geo_from_campaign(campaign, expected):
    assert detect_geo("adset-D1", campaign, KNOWN) == expected


def test_campaign_wins_over_adset():
    assert detect_geo("x_sk_y-D1", "[CZ] campaign", KNOWN) == "CZ"


def test_falls_back_to_adset_name():
    assert detect_geo("zdrave_sk_klouby-R2", "no geo here", KNOWN) == "SK"


def test_unknown_geo_not_returned():
    # DE есть в названии, но нет в порогах — не берём.
    assert detect_geo("x", "Germany Joints", KNOWN) is None


def test_candidates_surface_country_names():
    # Для диагностики: Czech виден как CZ, даже если его нет в порогах.
    assert "CZ" in geo_candidates("x", "Czech Republic Joints")


def test_candidates_dedupe():
    assert geo_candidates("cz_x-D1", "[CZ] czech joints") == ["CZ"]
