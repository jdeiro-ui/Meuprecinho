from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete

from .db import SessionLocal
from .models import Alert, SeenMessage, utcnow
from .telegram import manager

scheduler = AsyncIOScheduler(job_defaults={"coalesce": True, "max_instances": 1})


def _cleanup() -> None:
    with SessionLocal() as db:
        db.execute(delete(SeenMessage).where(SeenMessage.seen_at < utcnow() - timedelta(days=60)))
        db.execute(delete(Alert).where(Alert.created_at < utcnow() - timedelta(days=90)))
        db.commit()


def start() -> None:
    # Watchdog de reconexão (além do auto_reconnect do Telethon)
    scheduler.add_job(manager.ensure_connected, "interval", seconds=60, id="tg_watchdog")
    scheduler.add_job(_cleanup, "interval", hours=24, id="cleanup")
    scheduler.start()


def stop() -> None:
    scheduler.shutdown(wait=False)
