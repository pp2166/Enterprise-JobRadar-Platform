"""Edge-case coverage for crawlers + the BaseCrawler retry/semaphore logic.

All network is faked via a FakeClient; we cover: malformed payloads, missing
fields, bad dates, HTTP retries/backoff, final-failure behaviour, empty RSS,
and feed loop error isolation.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.crawlers.base import BaseCrawler, CrawlerRegistry
from app.crawlers.remoteok import RemoteOKCrawler
from app.crawlers.weworkremotely import WeWorkRemotelyCrawler


class _FakeResponse:
    def __init__(self, payload=None, text=None, status_code=200, raise_exc=None):
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self.status_code = status_code
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("bad json")
        return self._payload


class _FakeClient:
    """Minimal AsyncClient replacement driven by an injected url handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(url)
        out = self.handler(url, len(self.calls))
        if isinstance(out, Exception):
            raise out
        return out


class TestRemoteOKEdges:
    async def test_non_list_payload_yields_nothing(self, monkeypatch):
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload={"not": "a list"})),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs == []

    async def test_invalid_json_swallowed(self, monkeypatch):
        crawler = RemoteOKCrawler()
        resp = _FakeResponse(payload=ValueError(), text="{not json")
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: resp),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs == []

    async def test_missing_required_fields_skipped(self, monkeypatch):
        payload = [
            {"id": "1", "position": "", "company": "Acme"},            # empty title
            {"id": "2", "position": "Dev", "company": ""},             # empty company
            {"id": "3", "position": "Dev"},                            # missing company key
            {"position": "Dev", "company": "Acme"},                    # missing id (also missing from filter)
        ]
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=payload)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs == []

    async def test_malformed_date_does_not_crash(self, monkeypatch):
        payload = [
            {
                "id": "1",
                "position": "Dev",
                "company": "Acme",
                "description": "role description",
                "date": "not-a-real-date",
            }
        ]
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=payload)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert len(jobs) == 1
        assert jobs[0].posted_at is None  # falls through to None on parse failure

    async def test_epoch_integer_date_handled(self, monkeypatch):
        payload = [
            {
                "id": "1",
                "position": "Dev",
                "company": "Acme",
                "description": "role",
                "epoch": 1700000000,  # valid unix ts
            }
        ]
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=payload)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert len(jobs) == 1
        assert jobs[0].posted_at is not None

    async def test_tags_of_wrong_type_ignored(self, monkeypatch):
        payload = [
            {
                "id": "1",
                "position": "Dev",
                "company": "Acme",
                "description": "",
                "tags": ["python", 42, None, "go"],  # non-strings dropped
            }
        ]
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=payload)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs[0].tags == ["python", "go"]

    async def test_bad_salary_fields_ignored(self, monkeypatch):
        payload = [
            {
                "id": "1",
                "position": "Dev",
                "company": "Acme",
                "description": "",
                "salary_min": "garbage",
                "salary_max": None,
            }
        ]
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=payload)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs[0].salary_min is None
        assert jobs[0].salary_max is None

    async def test_default_url_when_missing(self, monkeypatch):
        payload = [
            {"id": "abc", "position": "Dev", "company": "Acme", "description": ""}
        ]
        crawler = RemoteOKCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=payload)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs[0].url.endswith("/abc")


class TestWWREdges:
    async def test_empty_rss_yields_nothing(self, monkeypatch):
        rss = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
        crawler = WeWorkRemotelyCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=None, text=rss)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs == []

    async def test_title_without_colon_becomes_unknown_company(self, monkeypatch):
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item>
            <title>Senior Engineer</title>
            <link>https://example.com/a</link>
            <guid>https://example.com/a</guid>
            <description>role</description>
          </item>
        </channel></rss>"""
        crawler = WeWorkRemotelyCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=None, text=rss)),
        )
        jobs = [j async for j in crawler.fetch()]
        # All 5 configured feeds return the same fixture → job appears 5×.
        assert len(jobs) >= 1
        assert all(j.company == "Unknown" for j in jobs)

    async def test_entry_missing_link_skipped(self, monkeypatch):
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item>
            <title>Acme: Engineer</title>
            <description>role</description>
          </item>
        </channel></rss>"""
        crawler = WeWorkRemotelyCrawler()
        monkeypatch.setattr(
            crawler, "_client",
            lambda: _FakeClient(lambda url, n: _FakeResponse(payload=None, text=rss)),
        )
        jobs = [j async for j in crawler.fetch()]
        assert jobs == []

    async def test_single_feed_failure_does_not_abort_others(self, monkeypatch):
        # First feed exhausts all 3 retries and is skipped; remaining feeds yield.
        ok_rss = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item>
            <title>Acme: Engineer</title>
            <link>https://example.com/1</link>
            <guid>https://example.com/1</guid>
            <description>role</description>
          </item>
        </channel></rss>"""

        def handler(url, n):
            # Calls 1–3 are the first feed's 3 retries; fail them all.
            if n <= 3:
                return httpx.ConnectError("down", request=None)
            return _FakeResponse(payload=None, text=ok_rss)

        crawler = WeWorkRemotelyCrawler()
        monkeypatch.setattr(crawler, "_client", lambda: _FakeClient(handler))
        # Neutralise retry sleep so the error case doesn't burn time.
        monkeypatch.setattr("app.crawlers.base.asyncio.sleep", _noop_sleep)

        jobs = [j async for j in crawler.fetch()]
        # 4 remaining feeds × 1 item each
        assert len(jobs) == 4


async def _noop_sleep(_):
    return None


class _DummyCrawler(BaseCrawler):
    name = "dummy"

    async def fetch(self):
        if False:
            yield  # pragma: no cover


class TestBaseCrawler:
    def test_subclass_without_name_raises(self):
        class NoName(BaseCrawler):
            async def fetch(self):
                if False:
                    yield

        with pytest.raises(ValueError):
            NoName()

    async def test_get_retries_then_succeeds(self, monkeypatch):
        crawler = _DummyCrawler()
        monkeypatch.setattr("app.crawlers.base.asyncio.sleep", _noop_sleep)

        def handler(url, n):
            if n < 3:
                return httpx.ConnectError("transient", request=None)
            return _FakeResponse(payload={"ok": True})

        client = _FakeClient(handler)
        resp = await crawler._get(client, "https://x")
        assert resp.json() == {"ok": True}
        assert len(client.calls) == 3  # failed 2x, succeeded on 3rd

    async def test_get_gives_up_after_three_attempts(self, monkeypatch):
        crawler = _DummyCrawler()
        monkeypatch.setattr("app.crawlers.base.asyncio.sleep", _noop_sleep)

        client = _FakeClient(lambda url, n: httpx.ConnectError("perma", request=None))
        with pytest.raises(httpx.HTTPError):
            await crawler._get(client, "https://x")
        assert len(client.calls) == 3

    async def test_semaphore_limits_concurrent_requests(self, monkeypatch):
        # Force concurrency=2, fire 5 parallel _get() calls, assert max in-flight ≤ 2.
        monkeypatch.setattr("app.crawlers.base.settings.crawl_concurrency", 2, raising=False)
        # Reconstruct so the semaphore sees the patched setting.
        crawler = _DummyCrawler()
        crawler._sem = asyncio.Semaphore(2)

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def handler_async(url, n):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return _FakeResponse(payload={"n": n})

        class _AsyncClient(_FakeClient):
            async def get(self, url, **kwargs):
                self.calls.append(url)
                return await handler_async(url, len(self.calls))

        client = _AsyncClient(lambda url, n: None)
        await asyncio.gather(*[crawler._get(client, f"https://x/{i}") for i in range(5)])
        assert peak <= 2


class TestRegistry:
    def test_get_unknown_raises(self):
        reg = CrawlerRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")

    def test_register_and_list(self):
        reg = CrawlerRegistry()
        c = _DummyCrawler()
        reg.register(c)
        assert reg.names() == ["dummy"]
        assert reg.all() == [c]
        assert reg.get("dummy") is c

    def test_re_register_overwrites(self):
        reg = CrawlerRegistry()
        reg.register(_DummyCrawler())
        second = _DummyCrawler()
        reg.register(second)
        assert reg.get("dummy") is second
        assert len(reg.names()) == 1
