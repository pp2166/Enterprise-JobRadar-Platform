"""Crawler unit tests — no network.

We drive the crawler from a fake httpx.AsyncClient so we can assert the
normalization behaviour on a stable fixture.
"""

from __future__ import annotations

import json

from app.crawlers.remoteok import RemoteOKCrawler
from app.crawlers.weworkremotely import WeWorkRemotelyCrawler


class _FakeResponse:
    def __init__(self, payload, text=None):
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        return self.handler(url)


async def test_remoteok_crawler_parses_listing(monkeypatch):
    payload = [
        {"legal": "header-meta-row", "disclaimer": "skip me"},
        {
            "id": "123",
            "position": "Senior Python Engineer",
            "company": "Acme",
            "location": "Remote",
            "tags": ["python", "fastapi"],
            "description": "<p>Build <b>async</b> backends. Salary $120k - $150k.</p>",
            "url": "https://remoteok.com/remote-jobs/123",
            "date": "2026-04-01T12:00:00Z",
            "salary_min": 120000,
            "salary_max": 150000,
        },
        {"not": "a job record"},
    ]

    crawler = RemoteOKCrawler()

    def fake_client_factory():
        return _FakeClient(lambda url: _FakeResponse(payload))

    monkeypatch.setattr(crawler, "_client", fake_client_factory)

    jobs = [j async for j in crawler.fetch()]
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "remoteok"
    assert j.source_id == "123"
    assert j.title == "Senior Python Engineer"
    assert j.company == "Acme"
    assert "async" in j.description.lower() and "<b>" not in j.description
    assert j.remote is True
    assert j.experience_level == "senior"
    assert j.salary_min == 120000 and j.salary_max == 150000
    assert j.posted_at is not None and j.posted_at.year == 2026


async def test_weworkremotely_parses_rss(monkeypatch):
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>WWR</title>
      <item>
        <title>Acme: Senior Rust Engineer</title>
        <link>https://weworkremotely.com/remote-jobs/xyz</link>
        <guid>https://weworkremotely.com/remote-jobs/xyz</guid>
        <description><![CDATA[<p>Join Acme to build a rust backend. Salary $140k - $170k.</p>]]></description>
        <pubDate>Tue, 01 Apr 2026 10:00:00 +0000</pubDate>
      </item>
    </channel></rss>"""

    crawler = WeWorkRemotelyCrawler()

    def fake_client_factory():
        return _FakeClient(lambda url: _FakeResponse(None, text=rss))

    monkeypatch.setattr(crawler, "_client", fake_client_factory)

    jobs = [j async for j in crawler.fetch()]
    assert len(jobs) >= 1
    j = jobs[0]
    assert j.source == "weworkremotely"
    assert j.company == "Acme"
    assert j.title == "Senior Rust Engineer"
    assert j.remote is True
    assert j.experience_level == "senior"
    assert j.salary_min == 140000 and j.salary_max == 170000
