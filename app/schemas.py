from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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


class CrawlResponse(BaseModel):
    dispatched: list[str]
