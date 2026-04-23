"""Persist normalized jobs, dedup via SimHash + upsert on (source, source_id)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Job
from app.services.dedup import compute_simhash, find_duplicate, to_signed
from app.services.normalize import NormalizedJob

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    received: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0


def _upsert_stmt(dialect_name: str, table, values: dict):
    """Build an INSERT ... ON CONFLICT DO UPDATE for postgres or sqlite.

    Both dialects speak upsert, but with different keyword args — we keep a
    single code path by branching on ``session.bind.dialect.name``.
    """
    update_cols_names = [k for k in values if k not in ("source", "source_id")]
    if dialect_name == "sqlite":
        stmt = sqlite_insert(table).values(**values)
        update_cols = {k: stmt.excluded[k] for k in update_cols_names}
        return stmt.on_conflict_do_update(
            index_elements=["source", "source_id"], set_=update_cols
        )
    stmt = pg_insert(table).values(**values)
    update_cols = {k: stmt.excluded[k] for k in update_cols_names}
    return stmt.on_conflict_do_update(
        constraint="uq_jobs_source_source_id", set_=update_cols
    )


async def ingest_jobs(session: AsyncSession, jobs: list[NormalizedJob]) -> IngestStats:
    stats = IngestStats(received=len(jobs))
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    for nj in jobs:
        unsigned = compute_simhash(nj.title, nj.company, nj.description)
        signed = to_signed(unsigned)

        existing_stmt = select(Job).where(
            Job.source == nj.source, Job.source_id == nj.source_id
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()

        if existing is None:
            dup = await find_duplicate(
                session,
                title=nj.title,
                company=nj.company,
                simhash_signed=signed,
                threshold=settings.simhash_threshold,
            )
            if dup is not None:
                stats.duplicates += 1
                log.debug(
                    "dedup: %s/%s collapsed into job=%s (dist=%s)",
                    nj.source,
                    nj.source_id,
                    dup.job_id,
                    dup.distance,
                )
                continue

        values = {
            "source": nj.source,
            "source_id": nj.source_id,
            "url": nj.url,
            "title": nj.title,
            "company": nj.company,
            "location": nj.location,
            "remote": nj.remote,
            "employment_type": nj.employment_type,
            "experience_level": nj.experience_level,
            "salary_min": nj.salary_min,
            "salary_max": nj.salary_max,
            "salary_currency": nj.salary_currency,
            "description": nj.description,
            "tags": ", ".join(nj.tags) if nj.tags else None,
            "simhash": signed,
            "posted_at": nj.posted_at,
        }

        stmt = _upsert_stmt(dialect_name, Job.__table__, values)
        await session.execute(stmt)
        if existing is None:
            stats.inserted += 1
        else:
            stats.updated += 1

    await session.commit()
    return stats
