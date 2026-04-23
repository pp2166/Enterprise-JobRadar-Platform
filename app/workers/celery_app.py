from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "jobhunt",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)

# Every 30 minutes, kick off a crawl of each source. Beat publishes; the
# worker pool runs them in parallel.
celery_app.conf.beat_schedule = {
    "crawl-remoteok": {
        "task": "app.workers.tasks.crawl_source",
        "schedule": crontab(minute="*/30"),
        "args": ("remoteok",),
    },
    "crawl-weworkremotely": {
        "task": "app.workers.tasks.crawl_source",
        "schedule": crontab(minute="5-59/30"),
        "args": ("weworkremotely",),
    },
}
