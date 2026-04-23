"""We Work Remotely crawler (RSS).

WWR exposes an RSS feed per category; we aggregate a few core ones. Descriptions
are HTML and get passed through the selectolax stripper in normalize.py.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import feedparser
from dateutil import parser as dtparser

from app.crawlers.base import BaseCrawler
from app.services.normalize import (
    NormalizedJob,
    ensure_utc,
    infer_experience,
    parse_salary,
    squish,
    strip_html,
)

log = logging.getLogger(__name__)

FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
]


class WeWorkRemotelyCrawler(BaseCrawler):
    name = "weworkremotely"

    async def fetch(self) -> AsyncIterator[NormalizedJob]:
        async with self._client() as client:
            for feed_url in FEEDS:
                try:
                    resp = await self._get(client, feed_url)
                except Exception as e:
                    log.warning("wwr: %s failed: %s", feed_url, e)
                    continue

                parsed = feedparser.parse(resp.text)
                for entry in parsed.entries:
                    nj = self._entry_to_normalized(entry)
                    if nj is not None:
                        yield nj

    def _entry_to_normalized(self, entry) -> NormalizedJob | None:
        link = getattr(entry, "link", None)
        guid = getattr(entry, "id", None) or link
        raw_title = squish(getattr(entry, "title", ""))
        if not link or not guid or not raw_title:
            return None

        # WWR titles are "Company: Title".
        if ":" in raw_title:
            company, _, title = raw_title.partition(":")
            company = squish(company)
            title = squish(title) or raw_title
        else:
            company = "Unknown"
            title = raw_title

        description = strip_html(getattr(entry, "summary", "") or getattr(entry, "description", ""))

        posted_at = None
        raw_date = getattr(entry, "published", None) or getattr(entry, "updated", None)
        if raw_date:
            try:
                posted_at = ensure_utc(dtparser.parse(raw_date))
            except (ValueError, TypeError):
                posted_at = None

        smin, smax, scur = parse_salary(description)
        tags = [t.term for t in getattr(entry, "tags", []) if getattr(t, "term", None)]

        return NormalizedJob(
            source=self.name,
            source_id=guid,
            url=link,
            title=title,
            company=company,
            description=description,
            location="Remote",
            remote=True,
            employment_type=None,
            experience_level=infer_experience(title, description),
            salary_min=smin,
            salary_max=smax,
            salary_currency=scur,
            tags=tags,
            posted_at=posted_at,
        )
