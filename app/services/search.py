"""Search + ranking.

Ranking formula (computed in SQL so we can order + paginate without pulling
rows):

    score =  ts_rank_cd(search_vector, query)
           + 0.5 * (title ILIKE '%q%')              -- small title-match boost
           + 1.0 * exp(-age_days / 14)              -- recency decay, half-life ≈ 14d

When no text query is supplied we fall back to pure recency order.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Float, and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job


@dataclass
class SearchFilters:
    q: str | None = None
    location: str | None = None
    remote: bool | None = None
    experience: str | None = None
    company: str | None = None
    source: str | None = None
    page: int = 1
    page_size: int = 20


def _build_tsquery(q: str):
    # websearch_to_tsquery handles quoted phrases, OR, and "-" negation for free.
    return func.websearch_to_tsquery("english", q)


def _filter_clauses(f: SearchFilters):
    clauses = []
    if f.location:
        clauses.append(Job.location.ilike(f"%{f.location}%"))
    if f.remote is True:
        clauses.append(Job.remote.is_(True))
    elif f.remote is False:
        clauses.append(or_(Job.remote.is_(False), Job.remote.is_(None)))
    if f.experience:
        clauses.append(Job.experience_level == f.experience)
    if f.company:
        clauses.append(Job.company.ilike(f"%{f.company}%"))
    if f.source:
        clauses.append(Job.source == f.source)
    return clauses


async def search_jobs(session: AsyncSession, f: SearchFilters) -> tuple[int, list[Job]]:
    page = max(1, f.page)
    page_size = max(1, min(100, f.page_size))
    offset = (page - 1) * page_size

    clauses = _filter_clauses(f)

    if f.q:
        tsq = _build_tsquery(f.q)
        clauses.append(Job.search_vector.op("@@")(tsq))

        # Recency: half-life 14d via exp(-age/14). `posted_at` may be NULL,
        # so fall back to fetched_at.
        age_days = func.extract(
            "epoch",
            func.now() - func.coalesce(Job.posted_at, Job.fetched_at),
        ) / literal(86400.0)

        recency = func.exp(-age_days / literal(14.0))
        text_score = func.ts_rank_cd(Job.search_vector, tsq)
        title_boost = case(
            (Job.title.ilike(f"%{f.q}%"), literal(0.5)),
            else_=literal(0.0),
        )
        score = (text_score + title_boost + recency).cast(Float).label("score")

        stmt = select(Job, score).where(and_(*clauses)).order_by(score.desc())
    else:
        stmt = (
            select(Job)
            .where(and_(*clauses) if clauses else True)
            .order_by(
                func.coalesce(Job.posted_at, Job.fetched_at).desc(),
                Job.id.desc(),
            )
        )

    count_stmt = select(func.count()).select_from(Job).where(and_(*clauses) if clauses else True)
    if f.q:
        # count must use the same tsquery match predicate
        count_stmt = (
            select(func.count())
            .select_from(Job)
            .where(and_(Job.search_vector.op("@@")(_build_tsquery(f.q)), *_filter_clauses(f)))
        )

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(stmt.offset(offset).limit(page_size))).all()
    # rows are tuples of (Job,) or (Job, score); first element is always the Job.
    jobs = [r[0] for r in rows]
    return total, jobs
