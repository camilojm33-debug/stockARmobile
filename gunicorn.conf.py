"""Gunicorn defaults for Render when Start Command omits CLI flags."""

import os


timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "2"))
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "gthread")