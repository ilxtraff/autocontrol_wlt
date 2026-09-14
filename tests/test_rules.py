"""Правила порогов — на реальных строках из журнала CRM."""
import pytest

from app.rules import (
    RULE_CPA, RULE_SPEND_NO_CONV, RULE_UCPC, Metrics, Thresholds,
    all_breaches, evaluate, is_back_to_normal,
)

IT = Thresholds(max_cpa=11.00, max_spend_no_conv=5.00, max_ucpc=0.2000)


@pytest.mark.parametrize(
    "metrics, expected",
    [
        # uCPC 0.2029 > 0.2000 при расходе всего $1.42 — сработать должен только uCPC.
        (Metrics(spend=1.42, conversions=0, unique_clicks=7), RULE_UCPC),
        # Расход $6.32 без единой конверсии.
        (Metrics(spend=6.32, conversions=0, unique_clicks=77), RULE_SPEND_NO_CONV),
        # CPA $13.66 при одном лиде.
        (Metrics(spend=13.66, conversions=1, leads=1, unique_clicks=125), RULE_CPA),
        # Всё в норме.
        (Metrics(spend=8.08, conversions=2, leads=2, unique_clicks=96), None),
        # Пустой адсет: ни расхода, ни кликов.
        (Metrics(), None),
    ],
)
def test_matches_crm_decisions(metrics, expected):
    breach = evaluate(metrics, IT)
    assert (breach.rule if breach else None) == expected


def test_spend_rule_switches_off_after_first_conversion():
    """Расход $14.90 больше порога $5, но лид уже есть — работает CPA, не расход."""
    metrics = Metrics(spend=14.90, conversions=1, leads=1, unique_clicks=134)
    breaches = {b.rule for b in all_breaches(metrics, IT)}
    assert RULE_SPEND_NO_CONV not in breaches
    assert RULE_CPA in breaches


def test_cpa_undefined_without_conversions():
    assert Metrics(spend=5.0, conversions=0).cpa is None


def test_ucpc_undefined_without_clicks():
    assert Metrics(spend=5.0, unique_clicks=0).ucpc is None


def test_rule_priority_is_cpa_then_spend_then_ucpc():
    """Когда превышено несколько правил, в журнал идёт первое по порядку."""
    metrics = Metrics(spend=30.0, conversions=1, leads=1, unique_clicks=10)
    assert evaluate(metrics, IT).rule == RULE_CPA
    assert len(all_breaches(metrics, IT)) == 2  # CPA и uCPC


def test_threshold_none_disables_rule():
    only_ucpc = Thresholds(max_cpa=None, max_spend_no_conv=None, max_ucpc=0.2)
    assert evaluate(Metrics(spend=999.0, conversions=0, unique_clicks=0), only_ucpc) is None


def test_exact_threshold_is_not_a_breach():
    """Порог — это «больше», а не «больше либо равно»."""
    assert evaluate(Metrics(spend=5.00, conversions=0, unique_clicks=1000), IT) is None
    assert evaluate(Metrics(spend=5.01, conversions=0, unique_clicks=1000), IT) is not None


def test_back_to_normal():
    assert is_back_to_normal(Metrics(spend=1.40, conversions=0, unique_clicks=7), IT)
    assert not is_back_to_normal(Metrics(spend=1.42, conversions=0, unique_clicks=7), IT)
