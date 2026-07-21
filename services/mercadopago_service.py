"""Cliente de integracion Mercado Pago (Checkout + consultas)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from urllib import request as urlrequest
from urllib.error import HTTPError
from typing import Any

from flask import current_app, has_app_context

from config.billing_config import load_billing_config


class MercadoPagoService:
    API_BASE = "https://api.mercadopago.com"

    def __init__(self):
        self.config = load_billing_config()

    def _logger(self):
        if has_app_context():
            return current_app.logger
        return logging.getLogger(__name__)

    def _headers(self, *, include_idempotency: bool = False, access_token: str | None = None) -> dict[str, str]:
        token = (access_token or self.config.access_token or "").strip()
        if not token:
            raise RuntimeError("MP_ACCESS_TOKEN no configurado")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if include_idempotency:
            headers["X-Idempotency-Key"] = str(uuid.uuid4())
        return headers

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None, access_token: str | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urlrequest.Request(
            url=f"{self.API_BASE}{path}",
            data=body,
            headers=self._headers(include_idempotency=method in {"POST", "PUT", "PATCH"}, access_token=access_token),
            method=method,
        )
        try:
            with urlrequest.urlopen(req, timeout=25) as response:
                raw = response.read().decode("utf-8")
                status_code = response.getcode()
        except HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else str(exc)
            raise RuntimeError(f"Mercado Pago error {exc.code}: {raw_error[:500]}") from exc

        if status_code >= 400:
            raise RuntimeError(f"Mercado Pago error {status_code}: {raw[:500]}")
        if not raw:
            return {}
        return json.loads(raw)

    def create_checkout_preference(
        self,
        *,
        title: str,
        amount: float,
        currency: str,
        external_reference: str,
        company_id: int,
        plan_id: int,
        subscription_id: int | None,
        user_id: int,
    ) -> dict[str, Any]:
        payload = {
            "items": [
                {
                    "id": str(plan_id),
                    "title": title,
                    "description": f"Suscripcion plan {title}",
                    "quantity": 1,
                    "currency_id": currency,
                    "unit_price": float(amount),
                }
            ],
            "external_reference": external_reference,
            "metadata": {
                "company_id": company_id,
                "plan_id": plan_id,
                "subscription_id": subscription_id,
                "user_id": user_id,
            },
            "back_urls": {
                "success": self.config.success_url,
                "pending": self.config.pending_url,
                "failure": self.config.failure_url,
            },
            "notification_url": self.config.notification_url,
            "statement_descriptor": self.config.statement_descriptor,
            "auto_return": "approved",
        }
        return self._request("POST", "/checkout/preferences", payload=payload)

    def create_pos_checkout_preference(
        self,
        *,
        title: str,
        amount: float,
        currency: str,
        external_reference: str,
        company_id: int,
        user_id: int,
        metadata: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "items": [
                {
                    "id": external_reference,
                    "title": title,
                    "description": "Cobro QR Mercado Pago desde POS",
                    "quantity": 1,
                    "currency_id": currency,
                    "unit_price": float(amount),
                }
            ],
            "external_reference": external_reference,
            "metadata": {
                "flow": "pos_sale",
                "company_id": company_id,
                "user_id": user_id,
                **(metadata or {}),
            },
            "back_urls": {
                "success": self.config.success_url,
                "pending": self.config.pending_url,
                "failure": self.config.failure_url,
            },
            "notification_url": self.config.notification_url,
            "statement_descriptor": self.config.statement_descriptor,
            "auto_return": "approved",
        }
        response = self._request("POST", "/checkout/preferences", payload=payload, access_token=access_token)
        self._logger().info(
            "MP POS checkout preference created: path=/checkout/preferences response_keys=%s has_init_point=%s has_sandbox_init_point=%s",
            sorted(response.keys()) if isinstance(response, dict) else type(response).__name__,
            bool(isinstance(response, dict) and response.get("init_point")),
            bool(isinstance(response, dict) and response.get("sandbox_init_point")),
        )
        return response

    @staticmethod
    def _extract_pos_results(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            results = payload.get("results")
            return results if isinstance(results, list) else []
        if isinstance(payload, list):
            return payload
        return []

    def list_pos_points(self, *, access_token: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        path = f"/pos?limit={int(limit)}&offset={int(offset)}"
        payload = self._request("GET", path, access_token=access_token)
        points: list[dict[str, Any]] = []
        for row in self._extract_pos_results(payload):
            if not isinstance(row, dict):
                continue
            store = row.get("store") if isinstance(row.get("store"), dict) else {}
            points.append(
                {
                    "id": str(row.get("id") or "").strip(),
                    "name": str(row.get("name") or row.get("title") or "POS").strip(),
                    "external_id": str(row.get("external_id") or "").strip(),
                    "store_id": str(row.get("store_id") or store.get("id") or "").strip(),
                    "store_name": str(row.get("store_name") or store.get("name") or "").strip(),
                    "status": str(row.get("status") or "").strip().lower(),
                }
            )
        return points

    def debug_fetch_pos_catalog(self, *, access_token: str | None = None) -> dict[str, Any]:
        """Diagnóstico temporal: consulta catálogo de POS para auditar respuestas vacías."""
        path = "/pos?limit=50&offset=0"
        req = urlrequest.Request(
            url=f"{self.API_BASE}{path}",
            headers=self._headers(access_token=access_token),
            method="GET",
        )
        status_code = None
        raw = ""
        try:
            with urlrequest.urlopen(req, timeout=25) as response:
                status_code = response.getcode()
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            status_code = exc.code
            raw = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else str(exc)

        payload = {}
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw[:1200]}

        pos_count = len(self._extract_pos_results(payload))

        return {
            "path": path,
            "status_code": status_code,
            "pos_count": pos_count,
            "response": payload,
        }

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/payments/{payment_id}")

    def get_preapproval(self, preapproval_id: str) -> dict[str, Any]:
        return self._request("GET", f"/preapproval/{preapproval_id}")

    def cancel_preapproval(self, preapproval_id: str) -> dict[str, Any]:
        return self._request("PUT", f"/preapproval/{preapproval_id}", payload={"status": "cancelled"})

    def validate_webhook_signature(self, *, request_id: str, x_signature: str, data_id: str) -> bool:
        secret = (self.config.webhook_secret or "").strip()
        if not secret:
            return self.config.mode != "production"
        if not x_signature:
            return False

        parts = dict(part.split("=", 1) for part in x_signature.split(",") if "=" in part)
        ts = parts.get("ts")
        v1 = parts.get("v1")
        if not ts or not v1 or not request_id:
            return False

        manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
        digest = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, v1)

    @staticmethod
    def parse_webhook_payload(raw_body: bytes) -> dict[str, Any]:
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))
