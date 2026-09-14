"""Определение гео по именам кампании и адсета.

Гео берётся в первую очередь из названия кампании Facebook, затем из имени
адсета. Понимаются разные написания: код в скобках `[CZ]`, отдельным куском
`cz` / `_cz_` / `CZ |`, а также названия стран словом («Czech», «Poland»).
Совпадение засчитывается только если такое гео заведено в порогах — иначе
случайные две буквы сошли бы за гео.
"""
from __future__ import annotations

import re

_BRACKET = re.compile(r"\[([a-z]{2})\]")
# Двухбуквенный код на границе слова: пробел, _, -, |, /, скобки, начало/конец.
_TOKEN = re.compile(r"(?:^|[\s_\-|/([{.,])([a-z]{2})(?=$|[\s_\-|/)\]}.,])")
# Код, склеенный с цифрами без разделителя: cz219499, hu3123123.
_CODE_DIGITS = re.compile(r"(?:^|[\s_\-|/([{.,])([a-z]{2})(?=\d)")

# Частые двухбуквенные куски, которые точно не гео.
_NOT_GEO = {"ad", "fb", "po", "ab", "id", "no", "v1", "v2", "wa", "ww"}

# Названия стран → ISO-код. Только то, что реально гоняют в affiliate.
_COUNTRIES = {
    "czech": "CZ", "czechia": "CZ", "czech republic": "CZ", "чехия": "CZ",
    "slovak": "SK", "slovakia": "SK", "словакия": "SK",
    "poland": "PL", "polska": "PL", "польша": "PL",
    "hungary": "HU", "венгрия": "HU",
    "romania": "RO", "румыния": "RO",
    "italy": "IT", "italia": "IT", "италия": "IT",
    "spain": "ES", "espana": "ES", "испания": "ES",
    "germany": "DE", "deutschland": "DE", "германия": "DE",
    "france": "FR", "франция": "FR",
    "portugal": "PT", "португалия": "PT",
    "mexico": "MX", "мексика": "MX",
    "greece": "GR", "греция": "GR",
    "bulgaria": "BG", "болгария": "BG",
    "croatia": "HR", "хорватия": "HR",
    "austria": "AT", "австрия": "AT",
    "netherlands": "NL", "нидерланды": "NL",
    "slovenia": "SI", "словения": "SI",
    "lithuania": "LT", "latvia": "LV", "estonia": "EE",
}


def _codes_in(text: str | None) -> list[str]:
    """Все гео-подобные коды из строки: скобки, отдельные куски, названия стран."""
    text = (text or "").lower()
    found: list[str] = []

    def add(code: str) -> None:
        code = code.upper()
        if code not in found:
            found.append(code)

    for m in _BRACKET.findall(text):
        if m not in _NOT_GEO:
            add(m)
    for m in _TOKEN.findall(text):
        if m not in _NOT_GEO:
            add(m)
    for m in _CODE_DIGITS.findall(text):
        if m not in _NOT_GEO:
            add(m)
    for name, code in _COUNTRIES.items():
        if name in text:
            add(code)
    return found


def detect_geo(
    adset_name: str | None, campaign_name: str | None, known_geos: set[str]
) -> str | None:
    """Гео из названия кампании, затем из имени адсета. None — не распознали."""
    known = {g.upper() for g in known_geos}
    for source in (campaign_name, adset_name):
        for code in _codes_in(source):
            if code in known:
                return code
    return None


def geo_candidates(adset_name: str | None, campaign_name: str | None) -> list[str]:
    """Все гео-подобные коды из имён — для диагностики, без сверки с порогами."""
    found: list[str] = []
    for source in (campaign_name, adset_name):
        for code in _codes_in(source):
            if code not in found:
                found.append(code)
    return found
