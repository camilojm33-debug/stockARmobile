"""Invoke the internal Mercado Pago billing reconciliation endpoint."""
from __future__ import annotations

import os
import sys

import requests


def _report_response(response: requests.Response) -> int:
    content_type = str(response.headers.get("Content-Type") or "").lower()
    request_id = str(
        response.headers.get("X-Request-ID")
        or response.headers.get("X-Correlation-ID")
        or ""
    ).strip()
    request_suffix = f" request_id={request_id}" if request_id else ""
    if not response.ok or "application/json" not in content_type:
        print(
            "Billing reconciliation endpoint failed: "
            f"HTTP {response.status_code}; content_type={content_type or 'unknown'}; "
            f"response body omitted.{request_suffix}",
            file=sys.stderr,
        )
        return 1
    try:
        payload = response.json()
    except ValueError:
        print("Billing reconciliation endpoint returned invalid JSON.", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("Billing reconciliation endpoint returned an invalid payload.", file=sys.stderr)
        return 1
    for key in ("events_scanned", "repaired", "ignored", "errors", "attention"):
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            print("Billing reconciliation endpoint returned invalid counters.", file=sys.stderr)
            return 1
    print(
        "Billing reconciliation completed: "
        f"events_scanned={payload['events_scanned']} repaired={payload['repaired']} "
        f"ignored={payload['ignored']} errors={payload['errors']} "
        f"attention={payload['attention']}"
    )
    if payload.get("ok") is not True:
        print("Billing reconciliation requires attention; response body omitted.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    base_url = (os.environ.get("STOCKARMOBILE_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("BILLING_RECONCILIATION_TOKEN") or "").strip()
    if not base_url or not token:
        print("Missing STOCKARMOBILE_BASE_URL or BILLING_RECONCILIATION_TOKEN.", file=sys.stderr)
        return 2
    try:
        response = requests.post(
            f"{base_url}/internal/maintenance/billing/reconcile",
            headers={"X-Billing-Reconciliation-Token": token},
            timeout=300,
        )
    except requests.RequestException as exc:
        print(
            f"Billing reconciliation request failed ({type(exc).__name__}); no response body available.",
            file=sys.stderr,
        )
        return 1
    return _report_response(response)


if __name__ == "__main__":
    raise SystemExit(main())
