"""Invoke the internal Gmail watch renewal endpoint from a Render cron job."""
from __future__ import annotations

import os
import sys

import requests


def _report_response(response: requests.Response) -> int:
    """Report safe renewal status without dumping HTML or sensitive response data."""
    content_type = str(response.headers.get("Content-Type") or "").lower()
    request_id = str(
        response.headers.get("X-Request-ID")
        or response.headers.get("X-Correlation-ID")
        or ""
    ).strip()
    request_suffix = f" request_id={request_id}" if request_id else ""

    if not response.ok or "application/json" not in content_type:
        print(
            "Gmail watch maintenance endpoint failed: "
            f"HTTP {response.status_code}; content_type={content_type or 'unknown'}; "
            f"response body omitted.{request_suffix}",
            file=sys.stderr,
        )
        return 1

    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        print("Gmail watch maintenance endpoint returned invalid JSON.", file=sys.stderr)
        return 1

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        print(
            f"Gmail watch renewal failed: HTTP {response.status_code}; ok=false; details omitted.{request_suffix}",
            file=sys.stderr,
        )
        return 1

    status = str(payload.get("status") or "unknown").strip().lower()
    if status not in {"renewed", "skipped"}:
        print("Gmail watch maintenance returned an unexpected status.", file=sys.stderr)
        return 1
    print(f"Gmail watch maintenance completed: ok=true status={status}")
    return 0


def main() -> int:
    base_url = (os.environ.get("STOCKARMOBILE_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("GMAIL_WATCH_AUTOMATION_TOKEN") or "").strip()
    if not base_url or not token:
        print("Missing STOCKARMOBILE_BASE_URL or GMAIL_WATCH_AUTOMATION_TOKEN.", file=sys.stderr)
        return 2

    try:
        response = requests.post(
            f"{base_url}/internal/maintenance/gmail/watch",
            headers={"X-Gmail-Watch-Automation-Token": token},
            timeout=120,
        )
    except requests.RequestException as exc:
        print(f"Gmail watch maintenance request failed ({type(exc).__name__}); no response body available.", file=sys.stderr)
        return 1
    return _report_response(response)


if __name__ == "__main__":
    raise SystemExit(main())
