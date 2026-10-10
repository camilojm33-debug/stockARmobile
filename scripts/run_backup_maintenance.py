"""Invoke the internal Render backup maintenance endpoint."""
from __future__ import annotations

import os
import sys

import requests


def _report_response(response: requests.Response) -> int:
    """Report only safe operational metadata; never echo an HTML/body response."""
    content_type = str(response.headers.get("Content-Type") or "").lower()
    request_id = str(
        response.headers.get("X-Request-ID")
        or response.headers.get("X-Correlation-ID")
        or ""
    ).strip()
    request_suffix = f" request_id={request_id}" if request_id else ""

    if not response.ok or "application/json" not in content_type:
        print(
            "Backup maintenance endpoint failed: "
            f"HTTP {response.status_code}; content_type={content_type or 'unknown'}; "
            f"response body omitted.{request_suffix}",
            file=sys.stderr,
        )
        return 1

    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        print("Backup maintenance endpoint returned invalid JSON.", file=sys.stderr)
        return 1

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        print(
            f"Backup maintenance failed: HTTP {response.status_code}; ok=false; details omitted.{request_suffix}",
            file=sys.stderr,
        )
        return 1

    rows = payload.get("results")
    rows = rows if isinstance(rows, list) else []
    counts = {
        status: sum(1 for row in rows if isinstance(row, dict) and row.get("status") == status)
        for status in ("ready", "skipped", "error")
    }
    verification = payload.get("restore_verification")
    verification_status = (
        "not_requested" if verification is None
        else "valid" if isinstance(verification, dict) and verification.get("valid") is True
        else "invalid"
    )
    processed = payload.get("companies_processed")
    processed = processed if isinstance(processed, int) and not isinstance(processed, bool) else len(rows)
    print(
        "Backup maintenance completed: ok=true "
        f"companies_processed={processed} ready={counts['ready']} "
        f"skipped={counts['skipped']} errors={counts['error']} "
        f"restore_verification={verification_status}"
    )
    return 0


def main() -> int:
    base_url = (os.environ.get("STOCKARMOBILE_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("BACKUP_AUTOMATION_TOKEN") or "").strip()
    if not base_url or not token:
        print("Missing STOCKARMOBILE_BASE_URL or BACKUP_AUTOMATION_TOKEN.", file=sys.stderr)
        return 2

    try:
        response = requests.post(
            f"{base_url}/internal/maintenance/backups/run",
            headers={"X-Backup-Automation-Token": token},
            timeout=300,
        )
    except requests.RequestException as exc:
        print(f"Backup maintenance request failed ({type(exc).__name__}); no response body available.", file=sys.stderr)
        return 1
    return _report_response(response)


if __name__ == "__main__":
    raise SystemExit(main())
