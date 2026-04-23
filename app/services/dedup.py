"""SimHash-based near-duplicate detection.

Strategy:
  - Compute a 64-bit SimHash over a normalized "fingerprint" string
    (title + company + first N words of description).
  - Store the hash in BIGINT. Postgres has no unsigned 64-bit int, so we map
    hashes >= 2^63 to negative by subtracting 2^64.
  - On insert, look up candidate duplicates by (lower(title), lower(company))
    and compare Hamming distance; if <= threshold, skip or merge.

This is dependency-light (only the `simhash` lib) and fast enough for a crawl
of a few thousand jobs; for larger scales, swap in a MinHash LSH index or
move this into an external service.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from simhash import Simhash
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

_MAX_U64 = 1 << 64
_MAX_I64 = 1 << 63

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _features(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def fingerprint_text(title: str, company: str, description: str, *, body_words: int = 120) -> str:
    body = " ".join(_features(description)[:body_words])
    return f"{title.lower().strip()} | {company.lower().strip()} | {body}"


def compute_simhash(title: str, company: str, description: str) -> int:
    features = _features(fingerprint_text(title, company, description))
    if not features:
        return 0
    return Simhash(features).value  # unsigned 64-bit


def to_signed(value: int) -> int:
    """Map unsigned 64-bit -> signed for BIGINT storage."""
    value &= _MAX_U64 - 1
    return value - _MAX_U64 if value >= _MAX_I64 else value


def to_unsigned(value: int) -> int:
    return value + _MAX_U64 if value < 0 else value


def hamming(a: int, b: int) -> int:
    return bin(to_unsigned(a) ^ to_unsigned(b)).count("1")


@dataclass
class DuplicateMatch:
    job_id: int
    distance: int


async def find_duplicate(
    session: AsyncSession,
    *,
    title: str,
    company: str,
    simhash_signed: int,
    threshold: int,
) -> DuplicateMatch | None:
    """Look for an existing job that is a near-duplicate.

    Uses a cheap (title, company) prefilter to avoid a full table scan; falls
    back to SimHash Hamming distance for the final decision. An exact company
    match with very similar title covers the common "same posting reposted to
    N boards" case.
    """
    stmt = select(Job.id, Job.simhash).where(
        func.lower(Job.company) == company.lower(),
    )
    # Narrow further when we have a title; postgres ILIKE handles case.
    if title:
        head = title.split(" ")[:4]
        stmt = stmt.where(Job.title.ilike(f"%{' '.join(head)}%"))
    stmt = stmt.limit(50)

    rows = (await session.execute(stmt)).all()
    best: DuplicateMatch | None = None
    for job_id, other_hash in rows:
        if other_hash is None:
            continue
        d = hamming(simhash_signed, other_hash)
        if d <= threshold and (best is None or d < best.distance):
            best = DuplicateMatch(job_id=job_id, distance=d)
    return best


def dedupe_batch(items: Iterable[tuple[str, str, str]], threshold: int = 3) -> list[int]:
    """Return indices of items to keep after intra-batch dedup.

    Useful when a single crawl pass returns the same job listed under multiple
    tags — we dedupe before hitting the DB.
    """
    keep: list[int] = []
    hashes: list[int] = []
    for idx, (title, company, description) in enumerate(items):
        h = compute_simhash(title, company, description)
        if any(hamming(to_signed(h), to_signed(other)) <= threshold for other in hashes):
            continue
        keep.append(idx)
        hashes.append(h)
    return keep
