from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    url: str
    title: str
    company: str
    location: str | None
    remote: bool | None
    employment_type: str | None
    experience_level: str | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    description: str
    tags: str | None
    posted_at: datetime | None
    fetched_at: datetime


class SearchResult(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[JobOut]


class CrawlRequest(BaseModel):
    source: str | None = None


class CrawlRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: int = Field(validation_alias="id")
    source: str
    status: str
    celery_task_id: str
    retry_of_run_id: int | None
    trigger_type: str
    attempt_count: int
    received: int
    inserted: int
    updated: int
    duplicates: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CrawlResponse(BaseModel):
    dispatched: list[str]
    runs: list[CrawlRunOut] = Field(default_factory=list)


class CrawlRunListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    runs: list[CrawlRunOut]
