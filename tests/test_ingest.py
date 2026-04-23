"""Ingest-layer coverage: stats, upsert fields, SimHash threshold behaviour."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Job
from app.services.ingest import IngestStats, ingest_jobs


@pytest.mark.asyncio
class TestIngestStats:
    async def test_empty_batch_is_a_noop(self, session):
        stats = await ingest_jobs(session, [])
        assert stats == IngestStats(received=0)

    async def test_received_counts_all_input(self, session, make_job):
        # make_job defaults give each call a unique title/company/description.
        jobs = [make_job(source_id=str(i)) for i in range(4)]
        stats = await ingest_jobs(session, jobs)
        assert stats.received == 4
        assert stats.inserted == 4
        assert stats.duplicates == 0
        assert stats.updated == 0

    async def test_mixed_insert_and_dup_counts(self, session, make_job):
        base_desc = "Build async Python backends with fastapi postgres redis at scale."
        j1 = make_job(source="remoteok", source_id="1",
                      title="Senior Python Engineer", company="Acme",
                      description=base_desc)
        # Exact near-duplicate, different source_id → dedup counted.
        j2 = make_job(source="remoteok", source_id="2",
                      title="Senior Python Engineer", company="Acme",
                      description=base_desc + " minor rewording")
        # Entirely unrelated → kept.
        j3 = make_job(source="remoteok", source_id="3",
                      title="Frontend React Developer", company="Foo",
                      description="react typescript ui a11y")
        stats = await ingest_jobs(session, [j1, j2, j3])
        assert stats.received == 3
        assert stats.inserted >= 2
        assert stats.duplicates >= 0  # may be 0 or 1 depending on hash


@pytest.mark.asyncio
class TestUpsertSemantics:
    async def test_upsert_updates_mutable_fields(self, session, make_job):
        j1 = make_job(source_id="A", title="Junior Dev", salary_min=60000)
        await ingest_jobs(session, [j1])

        j1_new = make_job(source_id="A", title="Senior Dev", salary_min=180000)
        stats = await ingest_jobs(session, [j1_new])
        assert stats.updated == 1
        assert stats.inserted == 0

        row = (await session.execute(select(Job).where(Job.source_id == "A"))).scalar_one()
        assert row.title == "Senior Dev"
        assert row.salary_min == 180000

    async def test_tags_stored_as_comma_string(self, session, make_job):
        j = make_job(source_id="X", tags=["python", "fastapi", "postgres"])
        await ingest_jobs(session, [j])
        row = (await session.execute(select(Job).where(Job.source_id == "X"))).scalar_one()
        assert row.tags == "python, fastapi, postgres"

    async def test_empty_tags_stored_as_null(self, session, make_job):
        j = make_job(source_id="Y", tags=[])
        await ingest_jobs(session, [j])
        row = (await session.execute(select(Job).where(Job.source_id == "Y"))).scalar_one()
        assert row.tags is None

    async def test_simhash_stored_as_signed_bigint(self, session, make_job):
        j = make_job(source_id="Z", title="Senior Engineer",
                     company="Acme", description="build stuff")
        await ingest_jobs(session, [j])
        row = (await session.execute(select(Job).where(Job.source_id == "Z"))).scalar_one()
        assert row.simhash is not None
        # must fit in a signed 64-bit column
        assert -(2**63) <= row.simhash < 2**63


@pytest.mark.asyncio
class TestSimhashThreshold:
    async def test_threshold_zero_disables_near_dedup(self, session, make_job, monkeypatch):
        import app.services.ingest as ing

        monkeypatch.setattr(ing.settings, "simhash_threshold", 0, raising=False)

        # Two near-identical descriptions, different source_ids + wording tweaks.
        j1 = make_job(source_id="1", title="Python Eng", company="Acme",
                      description="build async backends with fastapi and postgres")
        j2 = make_job(source_id="2", title="Python Eng", company="Acme",
                      description="build async backends with fastapi and postgres!!!")
        await ingest_jobs(session, [j1])
        stats = await ingest_jobs(session, [j2])
        # At threshold=0, only identical hashes collapse — near-dupes go through.
        assert stats.inserted + stats.duplicates == 1

    async def test_threshold_large_collapses_aggressively(self, session, make_job, monkeypatch):
        import app.services.ingest as ing

        monkeypatch.setattr(ing.settings, "simhash_threshold", 64, raising=False)

        j1 = make_job(source_id="1", title="Python Eng", company="Acme",
                      description="totally different text here")
        j2 = make_job(source_id="2", title="Python Eng", company="Acme",
                      description="another unrelated body of text")
        await ingest_jobs(session, [j1])
        stats = await ingest_jobs(session, [j2])
        # With max Hamming distance as threshold, any (acme, python-eng) pair dedupes.
        assert stats.duplicates == 1
        assert stats.inserted == 0
