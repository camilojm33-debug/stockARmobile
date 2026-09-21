"""Invoke the internal Render backup maintenance endpoint."""
from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    base_url = (os.environ.get("STOCKARMOBILE_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("BACKUP_AUTOMATION_TOKEN") or "").strip()
    if not base_url or not token:
        print("Missing STOCKARMOBILE_BASE_URL or BACKUP_AUTOMATION_TOKEN.", file=sys.stderr)
        return 2

    response = requests.post(
        f"{base_url}/internal/maintenance/backups/run",
        headers={"X-Backup-Automation-Token": token},
        timeout=300,
    )
    print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
