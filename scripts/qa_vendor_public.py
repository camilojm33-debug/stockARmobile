#!/usr/bin/env python3
"""Safe smoke checks for a published Vendor IA URL.

This script never creates a cart, order, payment, or sale. It only checks the
public page, catalog/state GET endpoints, and that an invalid mutation is
rejected. It is intended for a staging/public test URL after deployment.

Usage:
    python scripts/qa_vendor_public.py https://example.com vendedor-slug
"""
from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TIMEOUT = 15


def _get(url: str):
    request = Request(url, headers={"User-Agent": "StockARmobile-Vendor-QA/1.0"})
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def _post_json(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": "StockARmobile-Vendor-QA/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) != 3:
        print("Uso: python scripts/qa_vendor_public.py <base_url> <slug>")
        return 2

    base = sys.argv[1].rstrip("/")
    slug = sys.argv[2].strip()
    if not base or not slug:
        print("base_url y slug son obligatorios")
        return 2

    public_url = f"{base}/vendedor/{slug}"
    checks = [
        ("public_page", public_url, lambda status, body: status == 200 and "Vendedor IA" in body),
        ("catalog", f"{public_url}/catalog", lambda status, body: status == 200 and '"products"' in body),
        ("state", f"{public_url}/state", lambda status, body: status == 200 and '"success"' in body),
    ]

    failed = 0
    for name, url, validator in checks:
        try:
            status, body = _get(url)
            ok = validator(status, body)
            print(f"[{ 'OK' if ok else 'FAIL' }] {name}: HTTP {status}")
            if not ok:
                failed += 1
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"[FAIL] {name}: {exc}")
            failed += 1

    # Safe negative mutation: there is no conversation and therefore it must not
    # create anything or cross tenant boundaries.
    try:
        status, body = _post_json(
            f"{public_url}/order/cancel",
            {"conversation_id": 0, "order_number": "P-0", "confirm": True},
        )
        ok = status in {403, 404}
        print(f"[{ 'OK' if ok else 'FAIL' }] invalid_cancel_rejected: HTTP {status}")
        if not ok:
            failed += 1
    except (URLError, TimeoutError) as exc:
        print(f"[FAIL] invalid_cancel_rejected: {exc}")
        failed += 1

    if failed:
        print(f"Smoke QA finalizada con {failed} fallo(s).")
        return 1
    print("Smoke QA del Vendedor IA finalizada correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
