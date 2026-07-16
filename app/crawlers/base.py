from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import settings
from app.services.normalize import NormalizedJob

log = logging.getLogger(__name__)


class BaseCrawler(abc.ABC):
    """Each source implements `fetch()` as an async generator of NormalizedJob.

    The base class owns the HTTP client + concurrency semaphore so individual
    crawlers don't re-implement timeouts, UA, or retries.
    """

    name: str = ""

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} must set `name`")
        self._sem = asyncio.Semaphore(settings.crawl_concurrency)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=settings.crawl_timeout,
            headers={"User-Agent": settings.crawl_user_agent, "Accept": "*/*"},
            follow_redirects=True,
        )

    async def _get(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        async with self._sem:
            for attempt in range(3):
                try:
                    resp = await client.get(url, **kwargs)
                    resp.raise_for_status()
                    return resp
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    if attempt == 2:
                        raise
                    backoff = 0.5 * (2**attempt)
                    log.warning("fetch %s failed (%s); retrying in %.1fs", url, e, backoff)
                    await asyncio.sleep(backoff)
            raise RuntimeError("unreachable")

    @abc.abstractmethod
    async def fetch(self) -> AsyncIterator[NormalizedJob]: ...


class CrawlerRegistry:
    def __init__(self) -> None:
        self._items: dict[str, BaseCrawler] = {}

    def register(self, crawler: BaseCrawler) -> None:
        self._items[crawler.name] = crawler

    def get(self, name: str) -> BaseCrawler:
        if name not in self._items:
            raise KeyError(f"unknown crawler: {name}")
        return self._items[name]

    def all(self) -> list[BaseCrawler]:
        return list(self._items.values())

    def names(self) -> list[str]:
        return list(self._items.keys())
