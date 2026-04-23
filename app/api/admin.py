from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.crawlers import registry
from app.schemas import CrawlRequest, CrawlResponse
from app.workers.tasks import crawl_source

router = APIRouter()


@router.get("/sources")
async def list_sources() -> dict:
    return {"sources": registry.names()}


@router.post("/crawl", response_model=CrawlResponse)
async def trigger_crawl(req: CrawlRequest) -> CrawlResponse:
    if req.source:
        if req.source not in registry.names():
            raise HTTPException(status_code=400, detail=f"unknown source: {req.source}")
        crawl_source.delay(req.source)
        return CrawlResponse(dispatched=[req.source])

    dispatched = []
    for name in registry.names():
        crawl_source.delay(name)
        dispatched.append(name)
    return CrawlResponse(dispatched=dispatched)
