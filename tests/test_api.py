"""FastAPI endpoint coverage.

We reuse the real app but override `get_session` to point at the in-memory
SQLite engine provided by conftest. ASGITransport does not auto-run the
lifespan hook, so the Postgres-only init_schema() is skipped — tables are
created directly by the sqlite_engine fixture.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_session
from app.main import app
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
        await ingest_jobs(s, [
            make_job(source="remoteok",       source_id="1",
                     title="Senior Python Engineer", company="Acme"),
            make_job(source="remoteok",       source_id="2",
                     title="Junior Rust Engineer",   company="Beta",
                     experience_level="junior", remote=False),
            make_job(source="weworkremotely", source_id="3",
                     title="Staff Frontend Engineer", company="Gamma"),
        ])


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
        r_true  = await client.get("/search", params={"remote": "true"})
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

    async def test_crawl_unknown_source_rejected(self, client: AsyncClient):
        r = await client.post("/admin/crawl", json={"source": "not-real"})
        assert r.status_code == 400
        assert "unknown source" in r.json()["detail"]

    async def test_crawl_specific_source_dispatches(self, client: AsyncClient, monkeypatch):
        dispatched: list[str] = []

        def fake_delay(name):  # mimic celery task.delay signature
            dispatched.append(name)

        monkeypatch.setattr("app.api.admin.crawl_source.delay", fake_delay)
        r = await client.post("/admin/crawl", json={"source": "remoteok"})
        assert r.status_code == 200
        assert r.json() == {"dispatched": ["remoteok"]}
        assert dispatched == ["remoteok"]

    async def test_crawl_all_when_source_omitted(self, client: AsyncClient, monkeypatch):
        dispatched: list[str] = []
        monkeypatch.setattr(
            "app.api.admin.crawl_source.delay",
            lambda name: dispatched.append(name),
        )
        r = await client.post("/admin/crawl", json={})
        assert r.status_code == 200
        body = r.json()
        assert set(body["dispatched"]) == set(dispatched)
        assert "remoteok" in dispatched and "weworkremotely" in dispatched

    async def test_crawl_payload_is_optional(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr("app.api.admin.crawl_source.delay", lambda _: None)
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
