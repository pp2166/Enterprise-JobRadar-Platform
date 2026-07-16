"""Search-layer coverage (SQLite; filter + recency path).

The full-text ranking path exercises Postgres-specific functions
(websearch_to_tsquery, ts_rank_cd, tsvector @@) and is verified by the
docker-compose integration stack, not here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.ingest import ingest_jobs
from app.services.search import SearchFilters, search_jobs


@pytest.mark.asyncio
class TestFilters:
    async def test_empty_db_returns_no_results(self, session):
        total, jobs = await search_jobs(session, SearchFilters())
        assert total == 0 and jobs == []

    async def test_remote_true_filter(self, session, make_job, recent_timestamps):
        await ingest_jobs(
            session,
            [
                make_job(source_id="a", remote=True, posted_at=recent_timestamps["day_ago"]),
                make_job(source_id="b", remote=False, posted_at=recent_timestamps["hour_ago"]),
                make_job(source_id="c", remote=None, posted_at=recent_timestamps["now"]),
            ],
        )
        total, jobs = await search_jobs(session, SearchFilters(remote=True))
        assert total == 1
        assert jobs[0].source_id == "a"

    async def test_remote_false_filter_includes_nulls(self, session, make_job, recent_timestamps):
        await ingest_jobs(
            session,
            [
                make_job(source_id="a", remote=True, posted_at=recent_timestamps["day_ago"]),
                make_job(source_id="b", remote=False, posted_at=recent_timestamps["hour_ago"]),
                make_job(source_id="c", remote=None, posted_at=recent_timestamps["now"]),
            ],
        )
        total, _ = await search_jobs(session, SearchFilters(remote=False))
        # remote=False matches both false and NULL per implementation.
        assert total == 2

    async def test_experience_filter(self, session, make_job):
        await ingest_jobs(
            session,
            [
                make_job(source_id="s", experience_level="senior"),
                make_job(source_id="j", experience_level="junior"),
            ],
        )
        total, jobs = await search_jobs(session, SearchFilters(experience="senior"))
        assert total == 1 and jobs[0].experience_level == "senior"

    async def test_location_is_substring(self, session, make_job):
        await ingest_jobs(
            session,
            [
                make_job(source_id="a", location="Remote - United States"),
                make_job(source_id="b", location="Berlin, Germany"),
            ],
        )
        total, _ = await search_jobs(session, SearchFilters(location="states"))
        assert total == 1

    async def test_company_is_substring_case_insensitive(self, session, make_job):
        await ingest_jobs(
            session,
            [
                make_job(source_id="a", company="Acme Corp"),
                make_job(source_id="b", company="Globex"),
            ],
        )
        total, _ = await search_jobs(session, SearchFilters(company="ACME"))
        assert total == 1

    async def test_source_exact_match(self, session, make_job):
        await ingest_jobs(
            session,
            [
                make_job(source="remoteok", source_id="1"),
                make_job(source="weworkremotely", source_id="2"),
            ],
        )
        total, _ = await search_jobs(session, SearchFilters(source="weworkremotely"))
        assert total == 1

    async def test_filters_combine_conjunctively(self, session, make_job):
        await ingest_jobs(
            session,
            [
                make_job(source_id="a", company="Acme", remote=True, experience_level="senior"),
                make_job(source_id="b", company="Acme", remote=False, experience_level="senior"),
                make_job(source_id="c", company="Beta", remote=True, experience_level="senior"),
            ],
        )
        total, jobs = await search_jobs(
            session,
            SearchFilters(company="acme", remote=True, experience="senior"),
        )
        assert total == 1 and jobs[0].source_id == "a"


@pytest.mark.asyncio
class TestOrderingAndPagination:
    async def test_recency_order_newest_first(self, session, make_job, recent_timestamps):
        await ingest_jobs(
            session,
            [
                make_job(source_id="old", posted_at=recent_timestamps["month_ago"]),
                make_job(source_id="new", posted_at=recent_timestamps["now"]),
                make_job(source_id="mid", posted_at=recent_timestamps["day_ago"]),
            ],
        )
        _, jobs = await search_jobs(session, SearchFilters(page_size=10))
        assert [j.source_id for j in jobs] == ["new", "mid", "old"]

    async def test_pagination_walks_full_set(self, session, make_job, recent_timestamps):
        # Insert 5 jobs in predictable order (oldest → newest).
        deltas = [timedelta(days=i) for i in range(5, 0, -1)]
        now = recent_timestamps["now"]
        for i, d in enumerate(deltas):
            await ingest_jobs(
                session,
                [
                    make_job(source_id=f"j{i}", posted_at=now - d),
                ],
            )

        total, page1 = await search_jobs(session, SearchFilters(page=1, page_size=2))
        _, page2 = await search_jobs(session, SearchFilters(page=2, page_size=2))
        _, page3 = await search_jobs(session, SearchFilters(page=3, page_size=2))
        _, page4 = await search_jobs(session, SearchFilters(page=4, page_size=2))

        assert total == 5
        assert len(page1) == 2 and len(page2) == 2 and len(page3) == 1
        assert page4 == []  # past last page
        # No overlap / duplicates across pages.
        seen = {j.id for j in page1 + page2 + page3}
        assert len(seen) == 5

    async def test_page_size_clamped_to_100(self, session, make_job):
        await ingest_jobs(session, [make_job(source_id="a")])
        # page_size=9999 should be clamped internally and still return results.
        total, jobs = await search_jobs(session, SearchFilters(page_size=9999))
        assert total == 1 and len(jobs) == 1

    async def test_page_below_one_clamped(self, session, make_job):
        await ingest_jobs(session, [make_job(source_id="a")])
        total, jobs = await search_jobs(session, SearchFilters(page=0))
        assert total == 1 and len(jobs) == 1

    async def test_no_filters_no_query_lists_all(self, session, make_job, recent_timestamps):
        await ingest_jobs(
            session,
            [
                make_job(source_id="a", posted_at=recent_timestamps["now"]),
                make_job(source_id="b", posted_at=recent_timestamps["day_ago"]),
            ],
        )
        total, _ = await search_jobs(session, SearchFilters())
        assert total == 2


@pytest.mark.asyncio
class TestCrossFilterTotalsConsistent:
    async def test_total_matches_returned_len(self, session, make_job):
        await ingest_jobs(session, [make_job(source_id=str(i), company=f"Co{i}") for i in range(3)])
        total, jobs = await search_jobs(session, SearchFilters(company="Co"))
        assert total == len(jobs) == 3
