"""
scheduler.py — Background task scheduler using APScheduler.

Scheduled tasks run even when no WebSocket is open.
Results are saved to a new DB session and broadcast to connected clients.
A desktop notification fires when a task completes.
"""

import asyncio
import logging
import uuid
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url="sqlite:///emulAItor.db")},
    timezone="UTC",
)

# Broadcast callback — set by main.py to push events to all WebSocket clients
_broadcast: Callable[[dict], Awaitable[None]] | None = None


def set_broadcast(fn: Callable[[dict], Awaitable[None]]):
    global _broadcast
    _broadcast = fn


def start(broadcast_fn=None):
    if broadcast_fn:
        set_broadcast(broadcast_fn)
    if not _scheduler.running:
        _scheduler.start()
        logger.info("Scheduler started")


def stop():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


# ── job runner ────────────────────────────────────────────────────────────────

async def _run_task(task_description: str, user_id: str = None):
    """Run agent for a scheduled task. Saves result + notifies user."""
    import db
    import agent as ag

    session = await db.create_session(user_id=user_id)
    sid = session["id"]
    await db.update_session_title(sid, f"⏰ {task_description[:50]}")

    async def _send(payload: dict):
        payload["session_id"] = sid
        if _broadcast:
            await _broadcast(payload)

    try:
        await ag.run_agent(sid, task_description, _send, user_id=user_id)
    except Exception as exc:
        logger.error("Scheduled task failed: %s", exc)

    # Desktop notification
    try:
        from plyer import notification
        notification.notify(
            title="EmulAItor — Task Done",
            message=task_description[:80],
            app_name="EmulAItor",
            timeout=8,
        )
    except Exception:
        pass

    # Tell all clients to refresh their session list
    if _broadcast:
        await _broadcast({"type": "scheduled_task_done", "session_id": sid,
                          "task": task_description})


# ── public API ────────────────────────────────────────────────────────────────

def _parse_cron(expr: str) -> dict:
    """'min hour day month dow' → APScheduler cron kwargs."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-part cron expression, got: {expr!r}")
    minute, hour, day, month, day_of_week = parts
    return dict(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)


def add_job(task_description: str, cron_expr: str, user_id: str = None) -> str:
    job_id = f"task_{uuid.uuid4().hex[:8]}"
    _scheduler.add_job(
        _run_task,
        "cron",
        args=[task_description],
        kwargs={"user_id": user_id},
        id=job_id,
        replace_existing=False,
        **_parse_cron(cron_expr),
    )
    logger.info("Scheduled job %s: %r (%s) user=%s", job_id, task_description, cron_expr, user_id)
    return job_id


def list_jobs() -> list[dict]:
    return [
        {
            "id": j.id,
            "task": j.args[0] if j.args else "",
            "next_run": str(j.next_run_time) if j.next_run_time else "paused",
        }
        for j in _scheduler.get_jobs()
    ]


def remove_job(job_id: str) -> bool:
    try:
        _scheduler.remove_job(job_id)
        return True
    except Exception:
        return False
