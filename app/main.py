from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.admin import router as admin_router
from app.api.jobs import router as jobs_router
from app.config import settings
from app.schema import init_schema

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_schema()
    yield


app = FastAPI(title="jobhunt", version="0.1.0", lifespan=lifespan)
app.include_router(jobs_router, tags=["jobs"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
