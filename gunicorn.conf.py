"""Gunicorn defaults plus the proactive AI follow-up worker."""

from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import text


timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "2"))
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "gthread")

logger = logging.getLogger("stockarmobile.ai_followups")
_LOCK_KEY = 918273645


def _enabled() -> bool:
    value = str(os.getenv("AI_FOLLOWUP_WORKER_ENABLED", "true")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _interval_seconds() -> int:
    try:
        value = int(os.getenv("AI_FOLLOWUP_WORKER_INTERVAL_SECONDS", "3600"))
    except (TypeError, ValueError):
        value = 3600
    return max(300, min(value, 86400))


def _initial_delay_seconds() -> int:
    try:
        value = int(os.getenv("AI_FOLLOWUP_WORKER_INITIAL_DELAY_SECONDS", "60"))
    except (TypeError, ValueError):
        value = 60
    return max(5, min(value, 3600))


def _run_once(flask_app) -> None:
    from stockarmobile.extensions import db
    from services.ai_agent.followup_service import AIFollowupService

    def scan() -> None:
        with flask_app.app_context():
            result = AIFollowupService.scan()
            logger.info(
                "AI follow-up worker scanned=%s eligible=%s queued=%s",
                result["scanned"],
                result["eligible"],
                result["queued"],
            )

    try:
        with db.engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
                ).scalar()
            )
            if not acquired:
                return
            try:
                scan()
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
                )
                connection.commit()
    except Exception:
        # SQLite/dev does not support PostgreSQL advisory locks. Production
        # Render/Postgres uses the cross-worker lock above.
        try:
            scan()
        except Exception:
            logger.exception("AI follow-up worker scan failed")


def _worker(flask_app) -> None:
    time.sleep(_initial_delay_seconds())
    while True:
        _run_once(flask_app)
        time.sleep(_interval_seconds())


def post_worker_init(worker) -> None:
    """Start the proactive follow-up scheduler once in each Gunicorn worker."""
    if not _enabled():
        return

    flask_app = worker.app.wsgi()
    if getattr(flask_app, "config", {}).get("TESTING"):
        return
    if getattr(flask_app, "config", {}).get("IS_PYTEST_CONTEXT"):
        return

    thread = threading.Thread(
        target=_worker,
        args=(flask_app,),
        name="ai-followup-worker",
        daemon=True,
    )
    thread.start()
    logger.info("AI follow-up worker started in Gunicorn worker %s", worker.pid)
