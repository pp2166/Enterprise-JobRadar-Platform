"""Normalize heterogeneous job records into a single internal shape."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from selectolax.parser import HTMLParser

_WS_RE = re.compile(r"\s+")
_SALARY_RE = re.compile(
    r"\$?\s*(\d{2,3}(?:[,.]?\d{3})?)\s*(?:k|K)?\s*(?:-|to|–)\s*\$?\s*(\d{2,3}(?:[,.]?\d{3})?)\s*(?:k|K)?"
)

_SENIOR_TOKENS = ("senior", "sr.", "sr ", "staff", "principal", "lead")
_JUNIOR_TOKENS = ("junior", "jr.", "jr ", "entry", "intern", "graduate", "associate")
_MID_TOKENS = ("mid-level", "mid level", "intermediate")


@dataclass
class NormalizedJob:
    source: str
    source_id: str
    url: str
    title: str
    company: str
    description: str
    location: str | None = None
    remote: bool | None = None
    employment_type: str | None = None
    experience_level: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    tags: list[str] = field(default_factory=list)
    posted_at: datetime | None = None


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    try:
        text = HTMLParser(value).text(separator=" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    return _WS_RE.sub(" ", text).strip()


def squish(value: str | None) -> str:
    if not value:
        return ""
    return _WS_RE.sub(" ", value).strip()


def infer_remote(location: str | None, title: str, tags: list[str]) -> bool | None:
    haystack = " ".join(filter(None, [location or "", title, " ".join(tags)])).lower()
    if "remote" in haystack or "anywhere" in haystack or "distributed" in haystack:
        return True
    return None


def infer_experience(title: str, description: str) -> str | None:
    blob = f"{title}\n{description}".lower()
    if any(t in blob for t in _SENIOR_TOKENS):
        return "senior"
    if any(t in blob for t in _JUNIOR_TOKENS):
        return "junior"
    if any(t in blob for t in _MID_TOKENS):
        return "mid"
    return None


def parse_salary(text: str | None) -> tuple[int | None, int | None, str | None]:
    """Best-effort parse of a salary range out of free-form text.

    Returns (min, max, currency). Ranges are expanded to annual USD when "k" is
    present. Returns (None, None, None) if nothing plausible is found.
    """
    if not text:
        return None, None, None
    match = _SALARY_RE.search(text)
    if not match:
        return None, None, None
    low_s, high_s = match.group(1), match.group(2)

    def _to_int(s: str, k_hint: bool) -> int:
        v = int(re.sub(r"[,.\s]", "", s))
        if k_hint and v < 1000:
            v *= 1000
        return v

    has_k = "k" in match.group(0).lower()
    low = _to_int(low_s, has_k)
    high = _to_int(high_s, has_k)
    currency = "USD" if "$" in match.group(0) else None
    if low > high:
        low, high = high, low
    return low, high, currency


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
