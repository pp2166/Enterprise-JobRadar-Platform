"""FastAPI endpoint coverage.

We reuse the real app but override `get_session` to point at the in-memory
SQLite engine provided by conftest. ASGITransport does not auto-run the
lifespan hook, so the Postgres-only init_schema() is skipped — tables are
created directly by the sqlite_engine fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawlers import registry
from app.database import get_session
from app.main import app
from app.models import CrawlRun
from app.services.ingest import ingest_jobs


@pytest.fixture
async def client(sqlite_engine) -> AsyncIterator[AsyncClient]:
    Session = async_sessionmaker(sqlite_engine, expire_on_commit=False, class_=AsyncSession)

    async def override() -> AsyncIterator[AsyncSession]:
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def seeded(sqlite_engine, make_job):
    """Populate the SQLite engine with a small, deterministic job set."""
    Session = async_sessionmaker(sqlite_engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        await ingest_jobs(
            s,
            [
                make_job(
                    source="remoteok", source_id="1", title="Senior Python Engineer", company="Acme"
                ),
                make_job(
                    source="remoteok",
                    source_id="2",
                    title="Junior Rust Engineer",
                    company="Beta",
                    experience_level="junior",
                    remote=False,
                ),
                make_job(
                    source="weworkremotely",
                    source_id="3",
                    title="Staff Frontend Engineer",
                    company="Gamma",
                ),
            ],
        )


async def _create_crawl_run(
    session: AsyncSession,
    *,
    source: str,
    status: str,
    celery_task_id: str,
    created_at: datetime,
    trigger_type: str = "api",
    retry_of_run_id: int | None = None,
) -> CrawlRun:
    run = CrawlRun(
        source=source,
        status=status,
        celery_task_id=celery_task_id,
        created_at=created_at,
        trigger_type=trigger_type,
        retry_of_run_id=retry_of_run_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


def _crawl_run_snapshot(run: CrawlRun) -> dict[str, object]:
    return {
        "source": run.source,
        "status": run.status,
        "celery_task_id": run.celery_task_id,
        "retry_of_run_id": run.retry_of_run_id,
        "trigger_type": run.trigger_type,
        "attempt_count": run.attempt_count,
        "received": run.received,
        "inserted": run.inserted,
        "updated": run.updated,
        "duplicates": run.duplicates,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _active_conflict_detail(run: CrawlRun) -> dict[str, object]:
    return {
        "code": "ACTIVE_CRAWL_RUN_EXISTS",
        "message": f"active crawl run exists for source: {run.source}",
        "source": run.source,
        "active_run_id": run.id,
    }


@pytest.mark.asyncio
class TestHealth:
    async def test_healthz_returns_ok(self, client: AsyncClient):
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


@pytest.mark.asyncio
class TestSearchEndpoint:
    async def test_empty_results_shape(self, client: AsyncClient):
        r = await client.get("/search")
        assert r.status_code == 200
        body = r.json()
        assert body == {"total": 0, "page": 1, "page_size": 20, "results": []}

    async def test_returns_paged_result_envelope(self, client: AsyncClient, seeded):
        r = await client.get("/search", params={"page_size": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert body["page"] == 1
        assert body["page_size"] == 2
        assert len(body["results"]) == 2

    async def test_filter_by_company(self, client: AsyncClient, seeded):
        r = await client.get("/search", params={"company": "acme"})
        body = r.json()
        assert body["total"] == 1
        assert body["results"][0]["company"] == "Acme"

    async def test_filter_by_source(self, client: AsyncClient, seeded):
        r = await client.get("/search", params={"source": "weworkremotely"})
        body = r.json()
        assert body["total"] == 1
        assert body["results"][0]["source"] == "weworkremotely"

    async def test_filter_by_experience(self, client: AsyncClient, seeded):
        r = await client.get("/search", params={"experience": "junior"})
        body = r.json()
        assert body["total"] == 1
        assert body["results"][0]["experience_level"] == "junior"

    async def test_invalid_experience_rejected(self, client: AsyncClient):
        r = await client.get("/search", params={"experience": "ninja"})
        assert r.status_code == 422

    async def test_page_lt_one_rejected(self, client: AsyncClient):
        r = await client.get("/search", params={"page": 0})
        assert r.status_code == 422

    async def test_page_size_over_max_rejected(self, client: AsyncClient):
        r = await client.get("/search", params={"page_size": 1000})
        assert r.status_code == 422

    async def test_page_size_zero_rejected(self, client: AsyncClient):
        r = await client.get("/search", params={"page_size": 0})
        assert r.status_code == 422

    async def test_remote_bool_parsed(self, client: AsyncClient, seeded):
        r_true = await client.get("/search", params={"remote": "true"})
        r_false = await client.get("/search", params={"remote": "false"})
        assert r_true.status_code == 200 and r_false.status_code == 200
        # filter behaviour is unit-tested in test_search; just assert it's applied.
        assert r_true.json()["total"] + r_false.json()["total"] >= 3


@pytest.mark.asyncio
class TestJobDetailEndpoint:
    async def test_unknown_job_returns_404(self, client: AsyncClient):
        r = await client.get("/jobs/99999")
        assert r.status_code == 404
        assert r.json()["detail"] == "job not found"

    async def test_returns_job_by_id(self, client: AsyncClient, seeded):
        search = await client.get("/search", params={"company": "acme"})
        jid = search.json()["results"][0]["id"]
        r = await client.get(f"/jobs/{jid}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == jid and body["company"] == "Acme"

    async def test_non_integer_id_rejected(self, client: AsyncClient):
        r = await client.get("/jobs/abc")
        assert r.status_code == 422


@pytest.mark.asyncio
class TestAdminEndpoints:
    async def test_list_sources_returns_registered_names(self, client: AsyncClient):
        r = await client.get("/admin/sources")
        assert r.status_code == 200
        names = r.json()["sources"]
        assert "remoteok" in names and "weworkremotely" in names

    async def test_list_crawl_runs_empty_result(self, client: AsyncClient):
        r = await client.get("/admin/crawl-runs")
        assert r.status_code == 200
        assert r.json() == {
            "total": 0,
            "page": 1,
            "page_size": 20,
            "runs": [],
        }

    async def test_list_crawl_runs_orders_by_created_at_then_id_desc(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        older = await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-list-order-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        newer_first = await _create_crawl_run(
            session,
            source="remoteok",
            status="running",
            celery_task_id="task-api-list-order-002",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        newer_second = await _create_crawl_run(
            session,
            source="weworkremotely",
            status="succeeded",
            celery_task_id="task-api-list-order-003",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        r = await client.get("/admin/crawl-runs")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert body["runs"][0]["trigger_type"] == "api"
        assert body["runs"][0]["retry_of_run_id"] is None
        assert [run["run_id"] for run in body["runs"]] == [
            newer_second.id,
            newer_first.id,
            older.id,
        ]

    async def test_list_crawl_runs_filters_by_source(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        remoteok = await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-list-source-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="weworkremotely",
            status="queued",
            celery_task_id="task-api-list-source-002",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        r = await client.get("/admin/crawl-runs", params={"source": "remoteok"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert [run["run_id"] for run in body["runs"]] == [remoteok.id]

    async def test_list_crawl_runs_filters_by_status(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        succeeded = await _create_crawl_run(
            session,
            source="remoteok",
            status="succeeded",
            celery_task_id="task-api-list-status-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="remoteok",
            status="failed",
            celery_task_id="task-api-list-status-002",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        r = await client.get("/admin/crawl-runs", params={"status": "succeeded"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert [run["run_id"] for run in body["runs"]] == [succeeded.id]

    async def test_list_crawl_runs_filters_by_source_and_status(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        matching = await _create_crawl_run(
            session,
            source="remoteok",
            status="failed",
            celery_task_id="task-api-list-combined-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="remoteok",
            status="succeeded",
            celery_task_id="task-api-list-combined-002",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="weworkremotely",
            status="failed",
            celery_task_id="task-api-list-combined-003",
            created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

        r = await client.get(
            "/admin/crawl-runs",
            params={"source": "remoteok", "status": "failed"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert [run["run_id"] for run in body["runs"]] == [matching.id]

    async def test_list_crawl_runs_paginates_filtered_results(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        oldest = await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-list-page-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-list-page-002",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-list-page-003",
            created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="weworkremotely",
            status="queued",
            celery_task_id="task-api-list-page-004",
            created_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
        )

        r = await client.get(
            "/admin/crawl-runs",
            params={"source": "remoteok", "page": 2, "page_size": 2},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert body["page"] == 2
        assert body["page_size"] == 2
        assert [run["run_id"] for run in body["runs"]] == [oldest.id]

    @pytest.mark.parametrize(
        "params",
        [
            {"page": 0},
            {"page_size": 0},
            {"page_size": 101},
        ],
    )
    async def test_list_crawl_runs_rejects_invalid_pagination(
        self,
        client: AsyncClient,
        params: dict,
    ):
        r = await client.get("/admin/crawl-runs", params=params)
        assert r.status_code == 422

    async def test_get_crawl_run_returns_record(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        run = await _create_crawl_run(
            session,
            source="remoteok",
            status="succeeded",
            celery_task_id="task-api-get-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        r = await client.get(f"/admin/crawl-runs/{run.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == run.id
        assert body["source"] == "remoteok"
        assert body["status"] == "succeeded"
        assert body["celery_task_id"] == "task-api-get-001"
        assert body["trigger_type"] == "api"
        assert body["retry_of_run_id"] is None

    async def test_get_crawl_run_missing_returns_404(self, client: AsyncClient):
        r = await client.get("/admin/crawl-runs/999999")
        assert r.status_code == 404
        assert r.json()["detail"] == "crawl run not found: 999999"

    async def test_get_crawl_run_non_integer_rejected(self, client: AsyncClient):
        r = await client.get("/admin/crawl-runs/not-an-int")
        assert r.status_code == 422

    async def test_retry_failed_crawl_run_dispatches_new_run(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []

        def fake_apply_async(args, task_id):
            dispatched.append((args, task_id))

        parent = await _create_crawl_run(
            session,
            source="remoteok",
            status="failed",
            celery_task_id="task-api-retry-parent-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        original = {
            "id": parent.id,
            "source": parent.source,
            "status": parent.status,
            "celery_task_id": parent.celery_task_id,
            "retry_of_run_id": parent.retry_of_run_id,
            "trigger_type": parent.trigger_type,
        }
        monkeypatch.setattr("app.api.admin.crawl_source.apply_async", fake_apply_async)

        r = await client.post(f"/admin/crawl-runs/{parent.id}/retry")
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] != parent.id
        assert body["status"] == "queued"
        assert body["source"] == "remoteok"
        assert body["trigger_type"] == "manual"
        assert body["retry_of_run_id"] == parent.id
        assert dispatched == [(["remoteok"], body["celery_task_id"])]

        await session.refresh(parent)
        assert {
            "id": parent.id,
            "source": parent.source,
            "status": parent.status,
            "celery_task_id": parent.celery_task_id,
            "retry_of_run_id": parent.retry_of_run_id,
            "trigger_type": parent.trigger_type,
        } == original

    async def test_retry_failed_crawl_run_rejects_same_source_active_run(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []
        parent = await _create_crawl_run(
            session,
            source="remoteok",
            status="failed",
            celery_task_id="task-api-retry-active-parent-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        active = await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-retry-active-current-001",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        parent_original = _crawl_run_snapshot(parent)
        active_original = _crawl_run_snapshot(active)
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: dispatched.append((args, task_id)),
        )

        r = await client.post(f"/admin/crawl-runs/{parent.id}/retry")

        children = (
            await session.scalars(select(CrawlRun).where(CrawlRun.retry_of_run_id == parent.id))
        ).all()
        await session.refresh(parent)
        await session.refresh(active)

        assert r.status_code == 409
        assert r.json()["detail"] == _active_conflict_detail(active)
        assert dispatched == []
        assert children == []
        assert _crawl_run_snapshot(parent) == parent_original
        assert _crawl_run_snapshot(active) == active_original

    async def test_retry_non_failed_parent_prefers_not_retryable_over_activity(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ):
        parent = await _create_crawl_run(
            session,
            source="remoteok",
            status="succeeded",
            celery_task_id="task-api-retry-priority-parent-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _create_crawl_run(
            session,
            source="remoteok",
            status="queued",
            celery_task_id="task-api-retry-priority-active-001",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        r = await client.post(f"/admin/crawl-runs/{parent.id}/retry")

        assert r.status_code == 409
        assert r.json()["detail"] == {
            "code": "CRAWL_RUN_NOT_RETRYABLE",
            "message": f"crawl run is not retryable: {parent.id}",
            "run_id": parent.id,
            "status": "succeeded",
        }

    @pytest.mark.parametrize("status", ["queued", "running", "retrying", "succeeded"])
    async def test_retry_crawl_run_rejects_non_failed_status(
        self,
        client: AsyncClient,
        session: AsyncSession,
        status: str,
    ):
        run = await _create_crawl_run(
            session,
            source="remoteok",
            status=status,
            celery_task_id=f"task-api-retry-{status}-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        r = await client.post(f"/admin/crawl-runs/{run.id}/retry")
        assert r.status_code == 409
        assert r.json()["detail"] == {
            "code": "CRAWL_RUN_NOT_RETRYABLE",
            "message": f"crawl run is not retryable: {run.id}",
            "run_id": run.id,
            "status": status,
        }

    async def test_retry_crawl_run_missing_returns_404(self, client: AsyncClient):
        r = await client.post("/admin/crawl-runs/999999/retry")
        assert r.status_code == 404
        assert r.json()["detail"] == "crawl run not found: 999999"

    async def test_retry_crawl_run_non_integer_rejected(self, client: AsyncClient):
        r = await client.post("/admin/crawl-runs/not-an-int/retry")
        assert r.status_code == 422

    async def test_retry_crawl_run_dispatch_failure_marks_new_run_failed(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        def fake_apply_async(args, task_id):
            raise RuntimeError("broker unavailable")

        parent = await _create_crawl_run(
            session,
            source="remoteok",
            status="failed",
            celery_task_id="task-api-retry-fail-parent-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        monkeypatch.setattr("app.api.admin.crawl_source.apply_async", fake_apply_async)

        r = await client.post(f"/admin/crawl-runs/{parent.id}/retry")
        assert r.status_code == 503

        children = (
            await session.scalars(select(CrawlRun).where(CrawlRun.retry_of_run_id == parent.id))
        ).all()
        assert len(children) == 1
        child = children[0]
        assert r.json()["detail"] == f"failed to dispatch retry crawl run: {child.id}"
        assert child.status == "failed"
        assert child.trigger_type == "manual"
        assert child.retry_of_run_id == parent.id
        assert child.error_message is not None
        assert "dispatch failed: broker unavailable" in child.error_message

        await session.refresh(parent)
        assert parent.status == "failed"
        assert parent.retry_of_run_id is None

    async def test_crawl_unknown_source_rejected(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: dispatched.append((args, task_id)),
        )

        r = await client.post("/admin/crawl", json={"source": "not-real"})
        assert r.status_code == 400
        assert "unknown source" in r.json()["detail"]
        assert dispatched == []

        runs = (await session.scalars(select(CrawlRun))).all()
        assert runs == []

    @pytest.mark.parametrize("status", ["queued", "running", "retrying"])
    async def test_crawl_specific_source_rejects_same_source_active_run(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
        status: str,
    ):
        dispatched: list[tuple[list[str], str]] = []
        active = await _create_crawl_run(
            session,
            source="remoteok",
            status=status,
            celery_task_id=f"task-api-active-conflict-{status}-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        original = _crawl_run_snapshot(active)
        before = (await session.scalars(select(CrawlRun))).all()
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: dispatched.append((args, task_id)),
        )

        r = await client.post("/admin/crawl", json={"source": "remoteok"})

        after = (await session.scalars(select(CrawlRun))).all()
        await session.refresh(active)

        assert r.status_code == 409
        assert r.json()["detail"] == _active_conflict_detail(active)
        assert dispatched == []
        assert len(after) == len(before)
        assert _crawl_run_snapshot(active) == original

    @pytest.mark.parametrize("status", ["failed", "succeeded"])
    async def test_crawl_specific_source_allows_inactive_history(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
        status: str,
    ):
        dispatched: list[tuple[list[str], str]] = []
        await _create_crawl_run(
            session,
            source="remoteok",
            status=status,
            celery_task_id=f"task-api-inactive-history-{status}-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: dispatched.append((args, task_id)),
        )

        r = await client.post("/admin/crawl", json={"source": "remoteok"})

        assert r.status_code == 200
        body = r.json()
        assert body["dispatched"] == ["remoteok"]
        assert dispatched == [(["remoteok"], body["runs"][0]["celery_task_id"])]

        runs = (await session.scalars(select(CrawlRun))).all()
        assert len(runs) == 2

    async def test_crawl_specific_source_allows_different_source_active_run(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []
        await _create_crawl_run(
            session,
            source="weworkremotely",
            status="queued",
            celery_task_id="task-api-different-source-active-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: dispatched.append((args, task_id)),
        )

        r = await client.post("/admin/crawl", json={"source": "remoteok"})

        assert r.status_code == 200
        body = r.json()
        assert body["dispatched"] == ["remoteok"]
        assert dispatched == [(["remoteok"], body["runs"][0]["celery_task_id"])]

    async def test_crawl_specific_source_dispatches(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []

        def fake_apply_async(args, task_id):
            dispatched.append((args, task_id))

        monkeypatch.setattr("app.api.admin.crawl_source.apply_async", fake_apply_async)
        r = await client.post("/admin/crawl", json={"source": "remoteok"})
        assert r.status_code == 200
        body = r.json()
        assert body["dispatched"] == ["remoteok"]
        assert len(body["runs"]) == 1

        run = body["runs"][0]
        assert run["source"] == "remoteok"
        assert run["status"] == "queued"
        assert run["attempt_count"] == 0
        assert run["trigger_type"] == "api"
        assert run["retry_of_run_id"] is None
        assert isinstance(run["run_id"], int)
        assert dispatched == [(["remoteok"], run["celery_task_id"])]

        db_run = await session.get(CrawlRun, run["run_id"])
        assert db_run is not None
        assert db_run.source == "remoteok"
        assert db_run.status == "queued"
        assert db_run.celery_task_id == run["celery_task_id"]
        assert db_run.trigger_type == "api"

    async def test_crawl_specific_source_returns_queued_run_defaults(
        self,
        client: AsyncClient,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: None,
        )

        r = await client.post("/admin/crawl", json={"source": "remoteok"})
        assert r.status_code == 200
        run = r.json()["runs"][0]
        assert run["received"] == 0
        assert run["inserted"] == 0
        assert run["updated"] == 0
        assert run["duplicates"] == 0
        assert run["error_message"] is None
        assert run["created_at"] is not None
        assert run["started_at"] is None
        assert run["finished_at"] is None

    async def test_crawl_all_when_source_omitted(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []

        def fake_apply_async(args, task_id):
            dispatched.append((args, task_id))

        monkeypatch.setattr("app.api.admin.crawl_source.apply_async", fake_apply_async)
        r = await client.post("/admin/crawl", json={})
        assert r.status_code == 200
        body = r.json()
        source_names = registry.names()
        task_ids = [task_id for _, task_id in dispatched]

        assert set(body["dispatched"]) == set(source_names)
        assert [args[0] for args, _ in dispatched] == source_names
        assert len(task_ids) == len(set(task_ids))
        assert len(body["runs"]) == len(source_names)
        assert {run["source"] for run in body["runs"]} == set(source_names)
        assert {run["status"] for run in body["runs"]} == {"queued"}

        runs = (await session.scalars(select(CrawlRun))).all()
        assert len(runs) == len(source_names)
        assert {run.source for run in runs} == set(source_names)

    async def test_crawl_all_rejects_before_any_dispatch_when_source_active(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        dispatched: list[tuple[list[str], str]] = []
        active = await _create_crawl_run(
            session,
            source=registry.names()[0],
            status="queued",
            celery_task_id="task-api-crawl-all-active-001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        original = _crawl_run_snapshot(active)
        before = (await session.scalars(select(CrawlRun))).all()
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: dispatched.append((args, task_id)),
        )

        r = await client.post("/admin/crawl", json={})

        after = (await session.scalars(select(CrawlRun))).all()
        await session.refresh(active)

        assert r.status_code == 409
        assert r.json()["detail"] == _active_conflict_detail(active)
        assert dispatched == []
        assert len(after) == len(before)
        assert _crawl_run_snapshot(active) == original

    async def test_crawl_dispatch_failure_marks_run_failed(
        self,
        client: AsyncClient,
        monkeypatch,
        session: AsyncSession,
    ):
        def fake_apply_async(args, task_id):
            raise RuntimeError("broker unavailable")

        monkeypatch.setattr("app.api.admin.crawl_source.apply_async", fake_apply_async)
        r = await client.post("/admin/crawl", json={"source": "remoteok"})
        assert r.status_code == 503
        assert r.json()["detail"] == "failed to dispatch source: remoteok"

        runs = (await session.scalars(select(CrawlRun))).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.source == "remoteok"
        assert run.status == "failed"
        assert run.error_message is not None
        assert "broker unavailable" in run.error_message
        assert run.finished_at is not None

    async def test_crawl_payload_is_optional(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(
            "app.api.admin.crawl_source.apply_async",
            lambda args, task_id: None,
        )
        # Missing body is also fine — CrawlRequest has all-optional fields.
        r = await client.post("/admin/crawl", json={})
        assert r.status_code == 200


@pytest.mark.asyncio
class TestOpenAPI:
    async def test_openapi_schema_served(self, client: AsyncClient):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["info"]["title"] == "jobhunt"
        # Core endpoints are documented.
        assert "/search" in spec["paths"]
        assert "/jobs/{job_id}" in spec["paths"]
        assert "/admin/crawl" in spec["paths"]
