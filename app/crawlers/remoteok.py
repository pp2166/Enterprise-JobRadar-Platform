"""RemoteOK crawler.

Uses the public JSON feed at https://remoteok.com/api which returns the full
set of current listings in a single response. The first element is a header
record describing the feed; we skip it.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

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

FEED_URL = "https://remoteok.com/api"


class RemoteOKCrawler(BaseCrawler):
    name = "remoteok"

    async def fetch(self) -> AsyncIterator[NormalizedJob]:
        async with self._client() as client:
            resp = await self._get(client, FEED_URL)
            try:
                payload = resp.json()
            except ValueError:
                log.error("remoteok: bad JSON")
                return
        if not isinstance(payload, list):
            return

        for item in payload:
            if not isinstance(item, dict) or "id" not in item or "position" not in item:
                continue  # skip header/meta or malformed rows
            nj = self._to_normalized(item)
            if nj is not None:
                yield nj

    def _to_normalized(self, item: dict) -> NormalizedJob | None:
        title = squish(item.get("position"))
        company = squish(item.get("company"))
        if not title or not company:
            return None

        description = strip_html(item.get("description"))
        tags = [t for t in (item.get("tags") or []) if isinstance(t, str)]
        location = squish(item.get("location")) or None

        posted_raw = item.get("date") or item.get("epoch")
        posted_at: datetime | None = None
        if isinstance(posted_raw, str):
            try:
                posted_at = ensure_utc(dtparser.isoparse(posted_raw))
            except (ValueError, TypeError):
                posted_at = None
        elif isinstance(posted_raw, (int, float)):
            posted_at = datetime.fromtimestamp(float(posted_raw), tz=timezone.utc)

        smin, smax, scur = parse_salary(description)
        if item.get("salary_min"):
            try:
                smin = int(item["salary_min"])
            except (TypeError, ValueError):
                pass
        if item.get("salary_max"):
            try:
                smax = int(item["salary_max"])
            except (TypeError, ValueError):
                pass
        if not scur and (smin or smax):
            scur = "USD"

        url = item.get("url") or item.get("apply_url") or f"https://remoteok.com/remote-jobs/{item['id']}"

        return NormalizedJob(
            source=self.name,
            source_id=str(item["id"]),
            url=url,
            title=title,
            company=company,
            description=description,
            location=location,
            remote=True,  # RemoteOK is remote-only by definition
            employment_type=None,
            experience_level=infer_experience(title, description),
            salary_min=smin,
            salary_max=smax,
            salary_currency=scur,
            tags=tags,
            posted_at=posted_at,
        )
