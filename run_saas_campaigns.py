"""Render runner for SuperAdmin commercial campaigns.

Uses direct PostgreSQL when DATABASE_URL is available. If the cron service was
created without the database binding, it safely falls back to the authenticated
web worker so production never silently uses a private SQLite database.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

WORKER_URL = str(os.getenv("SAAS_CAMPAIGN_WORKER_URL") or "").strip()
WORKER_TOKEN = str(os.getenv("SAAS_CAMPAIGN_WORKER_TOKEN") or "").strip()



def _validated_worker_url(value: str) -> str | None:
    """Accept only HTTPS worker endpoints from trusted deployment configuration."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    allowed_hosts = {"www.stockarmobile.com", "stockarmobile.com"}
    if (
        parsed.scheme.lower() != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != "/internal/crm/campaign-worker"
        or parsed.query
        or (port is not None and port != 443)
    ):
        return None
    return raw


def _db_url_is_usable() -> bool:
    value = str(os.getenv("DATABASE_URL") or "").strip().lower()
    return bool(value and not value.startswith(("sqlite:", "sqlite3:")))


def _run_direct() -> int:
    from app import app, db
    from services.saas_commercial_service import dispatch_due_campaigns

    with app.app_context():
        result = dispatch_due_campaigns(db.session)
        print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def _run_via_web() -> int:
    worker_url = _validated_worker_url(WORKER_URL)
    if not worker_url or not WORKER_TOKEN:
        print(
            "Commercial worker unavailable: configure a valid HTTPS "
            "SAAS_CAMPAIGN_WORKER_URL and SAAS_CAMPAIGN_WORKER_TOKEN.",
            file=sys.stderr,
        )
        return 2

    request = urllib.request.Request(
        worker_url,
        method="POST",
        headers={
            "X-StockAr-Campaign-Worker-Token": WORKER_TOKEN,
            "Content-Type": "application/json",
            "User-Agent": "StockArMobile-CommercialCampaignWorker/1.0",
        },
        data=b"{}",
    )
    last_error = None
    for attempt in range(1, 4):
        try:
            # nosec B310: worker_url is rejected unless parsed as HTTPS above.
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - URL allowlist enforces the fixed HTTPS StockArMobile worker host/path.
                payload = response.read().decode("utf-8", errors="replace")
                print(payload)
                return 0 if 200 <= response.status < 300 else 1
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {502, 503, 504}:
                if attempt < 3:
                    time.sleep(10 * attempt)
                    continue
                print(
                    f"Commercial worker deferred: web service unavailable (HTTP {exc.code}); "
                    "next scheduled cycle will retry.",
                    file=sys.stderr,
                )
                return 0
            print(f"Commercial worker HTTP call failed: HTTP {exc.code}", file=sys.stderr)
            return 1
        except Exception as exc:
            last_error = exc
            message = str(exc)
            transient = any(code in message for code in ("502", "503", "504", "Bad Gateway", "Service Unavailable", "Gateway Timeout"))
            if transient:
                if attempt < 3:
                    time.sleep(10 * attempt)
                    continue
                print(
                    f"Commercial worker deferred: transient web outage ({message[:220]}); "
                    "next scheduled cycle will retry.",
                    file=sys.stderr,
                )
                return 0
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            print(f"Commercial worker HTTP call failed: {message[:500]}", file=sys.stderr)
            return 1
    print(f"Commercial worker HTTP call failed after retries: {str(last_error)[:500]}", file=sys.stderr)
    return 1


def main() -> int:
    if _db_url_is_usable():
        return _run_direct()
    return _run_via_web()


if __name__ == "__main__":
    raise SystemExit(main())
