"""Определение гео по именам кампании и адсета.

В именах из CRM гео стоит либо в скобках кампании — `[AK47] [PO] [it] [eblo2_it_ero]`,
либо отдельным куском имени адсета — `eblo2_it_ero-B4`, `t2in_ro_ero-R2`.
Берём только те двухбуквенные куски, для которых реально заведён порог, иначе
любое случайное сочетание букв сошло бы за гео.
"""
from __future__ import annotations

import re

_BRACKET = re.compile(r"\[([a-z]{2})\]")
_SEGMENT = re.compile(r"(?:^|[_\-])([a-z]{2})(?=[_\-])")


def detect_geo(
    adset_name: str | None, campaign_name: str | None, known_geos: set[str]
) -> str | None:
    """Гео из имени кампании, затем из имени адсета. None — не распознали."""
    known = {g.upper() for g in known_geos}

    for candidate in _BRACKET.findall((campaign_name or "").lower()):
        if candidate.upper() in known:
            return candidate.upper()

    for candidate in _SEGMENT.findall((adset_name or "").lower()):
        if candidate.upper() in known:
            return candidate.upper()

    return None
