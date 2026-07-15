"""Pydantic response-model + Settings coverage."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import CrawlRequest, CrawlResponse, CrawlRunOut, JobOut, SearchResult


class TestJobOut:
    def _valid_payload(self, **overrides) -> dict:
        data = {
            "id": 1,
            "source": "remoteok",
            "url": "https://example.com/1",
            "title": "Dev",
            "company": "Acme",
            "location": "Remote",
            "remote": True,
            "employment_type": None,
            "experience_level": "senior",
            "salary_min": 100000,
            "salary_max": 150000,
            "salary_currency": "USD",
            "description": "role",
            "tags": "python, fastapi",
            "posted_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "fetched_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        }
        data.update(overrides)
        return data

    def test_round_trip(self):
        jo = JobOut(**self._valid_payload())
        assert jo.id == 1
        assert jo.salary_currency == "USD"

    def test_optional_fields_accept_none(self):
        jo = JobOut(**self._valid_payload(
            location=None, remote=None, employment_type=None,
            experience_level=None, salary_min=None, salary_max=None,
            salary_currency=None, tags=None, posted_at=None,
        ))
        assert jo.posted_at is None
        assert jo.salary_min is None

    def test_missing_required_field_raises(self):
        payload = self._valid_payload()
        payload.pop("title")
        with pytest.raises(ValidationError):
            JobOut(**payload)

    def test_from_attributes_works_with_orm_like_object(self):
        class Obj:
            pass

        obj = Obj()
        for k, v in self._valid_payload().items():
            setattr(obj, k, v)

        jo = JobOut.model_validate(obj)
        assert jo.title == "Dev"


class TestSearchResult:
    def test_empty_result(self):
        sr = SearchResult(total=0, page=1, page_size=20, results=[])
        assert sr.total == 0

    def test_nested_jobs(self):
        job_dict = {
            "id": 1, "source": "remoteok", "url": "u", "title": "t",
            "company": "c", "location": None, "remote": None,
            "employment_type": None, "experience_level": None,
            "salary_min": None, "salary_max": None, "salary_currency": None,
            "description": "", "tags": None, "posted_at": None,
            "fetched_at": datetime.now(timezone.utc),
        }
        sr = SearchResult(total=1, page=1, page_size=20, results=[JobOut(**job_dict)])
        assert len(sr.results) == 1


class TestCrawlRequestResponse:
    def _valid_run_payload(self, **overrides) -> dict:
        data = {
            "id": 42,
            "source": "remoteok",
            "status": "succeeded",
            "celery_task_id": "task-1",
            "attempt_count": 2,
            "received": 10,
            "inserted": 7,
            "updated": 2,
            "duplicates": 1,
            "error_message": None,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "started_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            "finished_at": datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
        }
        data.update(overrides)
        return data

    def test_request_source_optional(self):
        assert CrawlRequest().source is None
        assert CrawlRequest(source="remoteok").source == "remoteok"

    def test_response_requires_list(self):
        assert CrawlResponse(dispatched=[]).dispatched == []
        assert CrawlResponse(dispatched=["a", "b"]).dispatched == ["a", "b"]
        with pytest.raises(ValidationError):
            CrawlResponse(dispatched="a")  # type: ignore[arg-type]

    def test_response_defaults_runs_to_empty_list(self):
        response = CrawlResponse(dispatched=["task-1"])
        assert response.runs == []
        assert response.model_dump() == {
            "dispatched": ["task-1"],
            "runs": [],
        }

    def test_run_out_uses_id_as_run_id(self):
        run = CrawlRunOut(**self._valid_run_payload())
        assert run.run_id == 42

    def test_response_can_include_runs(self):
        run = CrawlRunOut(**self._valid_run_payload(id=7))
        response = CrawlResponse(dispatched=["task-1"], runs=[run])
        assert response.runs[0].run_id == 7

    def test_run_dump_contains_run_id_not_id(self):
        run = CrawlRunOut(**self._valid_run_payload())
        dumped = run.model_dump()
        assert dumped["run_id"] == 42
        assert "id" not in dumped


class TestSettings:
    def test_defaults_are_reasonable(self):
        s = Settings()
        assert s.database_url.startswith("postgresql+asyncpg://")
        assert s.sync_database_url.startswith("postgresql+psycopg2://")
        assert s.redis_url.startswith("redis://")
        assert s.crawl_concurrency >= 1
        assert s.crawl_timeout >= 1
        assert s.simhash_threshold >= 0

    def test_extra_env_vars_ignored(self, monkeypatch):
        # model_config sets extra="ignore" — unknown vars must not raise.
        monkeypatch.setenv("TOTALLY_UNRELATED", "x")
        s = Settings()
        assert s  # doesn't raise

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("SIMHASH_THRESHOLD", "7")
        s = Settings(_env_file=None)  # bypass .env file
        assert s.database_url == "sqlite+aiosqlite:///:memory:"
        assert s.simhash_threshold == 7

    def test_invalid_int_raises(self, monkeypatch):
        monkeypatch.setenv("SIMHASH_THRESHOLD", "not-an-int")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
