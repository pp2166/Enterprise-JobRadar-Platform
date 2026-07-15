from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Text on non-Postgres backends (so tests can use SQLite), TSVECTOR on Postgres
# where the trigger in app/schema.py keeps it populated for FTS.
_SearchVectorType = Text().with_variant(TSVECTOR(), "postgresql")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(1024))

    title: Mapped[str] = mapped_column(String(512), index=True)
    company: Mapped[str] = mapped_column(String(256), index=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    remote: Mapped[bool | None] = mapped_column(nullable=True, index=True)
    employment_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    experience_level: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    salary_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 64-bit SimHash stored signed to fit in BIGINT (postgres has no unsigned).
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Populated by a Postgres trigger installed in schema.py.
    search_vector = Column(_SearchVectorType)

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_jobs_source_source_id"),
        Index("ix_jobs_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_jobs_posted_at_desc", posted_at.desc()),
    )

class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    celery_task_id: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
    )

    attempt_count: Mapped[int] = mapped_column(default=0)

    received: Mapped[int] = mapped_column(default=0)
    inserted: Mapped[int] = mapped_column(default=0)
    updated: Mapped[int] = mapped_column(default=0)
    duplicates: Mapped[int] = mapped_column(default=0)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_crawl_runs_created_at_desc", created_at.desc()),
    )