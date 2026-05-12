from __future__ import annotations

import re


_WEAR_SUFFIX_RE = re.compile(
    r"\s*\((factory new|minimal wear|field-tested|well-worn|battle-scarred)\)\s*$",
    re.IGNORECASE,
)
_STAT_PREFIX_RE = re.compile(r"^\s*stattrak(?:tm|™)?\s+", re.IGNORECASE)
_SOUVENIR_PREFIX_RE = re.compile(r"^\s*souvenir\s+", re.IGNORECASE)
_EXTERIOR_PREFIX_RE = re.compile(r"^\s*★\s*")
_SPACES_RE = re.compile(r"\s+")


def risk_category_from_market_hash_name(market_hash_name: str) -> str:
    name = str(market_hash_name or "").strip()
    if not name:
        return ""
    normalized = _SPACES_RE.sub(" ", name)
    normalized = _EXTERIOR_PREFIX_RE.sub("", normalized)
    normalized = _STAT_PREFIX_RE.sub("", normalized)
    normalized = _SOUVENIR_PREFIX_RE.sub("", normalized)
    normalized = _WEAR_SUFFIX_RE.sub("", normalized).strip()
    return normalized.casefold()
