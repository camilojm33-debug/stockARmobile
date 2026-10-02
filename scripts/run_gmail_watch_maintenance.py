"""Invoke the internal Gmail watch renewal endpoint from a Render cron job."""
from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    base_url = (os.environ.get("STOCKARMOBILE_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("GMAIL_WATCH_AUTOMATION_TOKEN") or "").strip()
    if not base_url or not token:
        print("Missing STOCKARMOBILE_BASE_URL or GMAIL_WATCH_AUTOMATION_TOKEN.", file=sys.stderr)
        return 2

    response = requests.post(
        f"{base_url}/internal/maintenance/gmail/watch",
        headers={"X-Gmail-Watch-Automation-Token": token},
        timeout=120,
    )
    print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
