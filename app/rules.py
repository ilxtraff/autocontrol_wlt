"""Пороги и их проверка.

Три правила, ровно как в CRM:
  • CPA                 — расход / лиды, считается только когда лид уже есть;
  • расход без конверсий — расход при нуле конверсий;
  • uCPC                — расход / уникальные клики.

Порядок проверки совпадает с порядком колонок в CRM: CPA → расход → uCPC.
Первое сработавшее правило и попадает в журнал.
"""
from __future__ import annotations

from dataclasses import dataclass

RULE_CPA = "cpa"
RULE_SPEND_NO_CONV = "spend_no_conv"
RULE_UCPC = "ucpc"

RULE_TITLES = {
    RULE_CPA: "CPA",
    RULE_SPEND_NO_CONV: "расход без конв.",
    RULE_UCPC: "uCPC",
}

RULE_ORDER = (RULE_CPA, RULE_SPEND_NO_CONV, RULE_UCPC)


@dataclass(frozen=True)
class Thresholds:
    """Пороги адсета. None — правило выключено."""

    max_cpa: float | None = None
    max_spend_no_conv: float | None = None
    max_ucpc: float | None = None

    def is_empty(self) -> bool:
        return not any((self.max_cpa, self.max_spend_no_conv, self.max_ucpc))

    def limit_for(self, rule: str) -> float | None:
        return {
            RULE_CPA: self.max_cpa,
            RULE_SPEND_NO_CONV: self.max_spend_no_conv,
            RULE_UCPC: self.max_ucpc,
        }[rule]


@dataclass(frozen=True)
class Metrics:
    """Срез статистики из Keitaro за окно рекламного дня."""

    spend: float = 0.0
    conversions: int = 0
    leads: int = 0
    sales: int = 0
    clicks: int = 0
    unique_clicks: int = 0
    revenue: float = 0.0

    @property
    def cpa(self) -> float | None:
        """Расход на конверсию. None — конверсий ещё нет, CPA не определён."""
        if self.conversions <= 0:
            return None
        return self.spend / self.conversions

    @property
    def ucpc(self) -> float | None:
        """Расход на уникальный клик. None — кликов ещё нет."""
        if self.unique_clicks <= 0:
            return None
        return self.spend / self.unique_clicks

    def value_for(self, rule: str) -> float | None:
        if rule == RULE_CPA:
            return self.cpa
        if rule == RULE_SPEND_NO_CONV:
            return self.spend
        if rule == RULE_UCPC:
            return self.ucpc
        raise ValueError(f"неизвестное правило: {rule}")


@dataclass(frozen=True)
class Breach:
    """Сработавшее правило — с числами на момент решения."""

    rule: str
    value: float
    limit: float

    @property
    def title(self) -> str:
        return RULE_TITLES[self.rule]


def check_rule(rule: str, metrics: Metrics, thresholds: Thresholds) -> Breach | None:
    """Проверяет одно правило. None — порог не задан, не применим или не превышен."""
    limit = thresholds.limit_for(rule)
    if limit is None or limit <= 0:
        return None

    if rule == RULE_SPEND_NO_CONV:
        # Правило живёт только до первой конверсии: как только лид долетел,
        # расход оценивается уже через CPA.
        if metrics.conversions > 0:
            return None
        value = metrics.spend
    else:
        value = metrics.value_for(rule)
        if value is None:
            return None

    if value > limit:
        return Breach(rule=rule, value=value, limit=limit)
    return None


def evaluate(metrics: Metrics, thresholds: Thresholds) -> Breach | None:
    """Первое превышение в порядке CPA → расход без конверсий → uCPC."""
    for rule in RULE_ORDER:
        breach = check_rule(rule, metrics, thresholds)
        if breach is not None:
            return breach
    return None


def all_breaches(metrics: Metrics, thresholds: Thresholds) -> list[Breach]:
    """Все сработавшие правила — для подсказок в интерфейсе."""
    found = [check_rule(rule, metrics, thresholds) for rule in RULE_ORDER]
    return [breach for breach in found if breach is not None]


def is_back_to_normal(metrics: Metrics, thresholds: Thresholds) -> bool:
    """Метрики вернулись в норму — ни одно правило больше не превышено."""
    return evaluate(metrics, thresholds) is None
