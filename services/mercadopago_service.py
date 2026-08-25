"""Cliente de integracion Mercado Pago (Checkout + consultas)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import uuid
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from typing import Any

from flask import current_app, has_app_context

from config.billing_config import load_billing_config


class MercadoPagoService:
    API_BASE = "https://api.mercadopago.com"
    REQUEST_TIMEOUT_SECONDS = 10

    def __init__(self):
        self.config = load_billing_config()

    def _logger(self):
        if has_app_context(): return current_app.logger
        return logging.getLogger(__name__)

    def _headers(self, *, include_idempotency: bool = False, access_token: str | None = None) -> dict[str, str]:
        token = (access_token or self.config.access_token or "").strip()
        if not token: raise RuntimeError("MP_ACCESS_TOKEN no configurado")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StockArMobile/1.0",
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
            self._logger().info("Mercado Pago request: method=%s path=%s timeout=%ss", method, path, self.REQUEST_TIMEOUT_SECONDS)
            with urlrequest.urlopen(req, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
                status_code = response.getcode()
        except HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else str(exc)
            self._logger().error("Mercado Pago HTTP error: method=%s path=%s status=%s body=%s", method, path, exc.code, raw_error[:1500])
            raise RuntimeError(f"Mercado Pago rechazó la operación (HTTP {exc.code}). {raw_error[:700]}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            self._logger().exception("Mercado Pago connection error: method=%s path=%s reason=%s", method, path, reason)
            raise RuntimeError(
                f"Mercado Pago no respondió dentro de {self.REQUEST_TIMEOUT_SECONDS} segundos. Verificá MP_ACCESS_TOKEN, MP_MODE y la conectividad de Render."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Mercado Pago devolvió una respuesta inválida.") from exc
        if status_code >= 400:
            raise RuntimeError(f"Mercado Pago respondió HTTP {status_code}: {raw[:700]}")
        return json.loads(raw) if raw else {}

    def create_checkout_preference(self, *, title: str, amount: float, currency: str, external_reference: str, company_id: int, plan_id: int, subscription_id: int | None, user_id: int) -> dict[str, Any]:
        payload = {"items": [{"id": str(plan_id), "title": title, "description": f"Suscripcion plan {title}", "quantity": 1, "currency_id": currency, "unit_price": float(amount)}], "external_reference": external_reference, "metadata": {"company_id": company_id, "plan_id": plan_id, "subscription_id": subscription_id, "user_id": user_id}, "back_urls": {"success": self.config.success_url, "pending": self.config.pending_url, "failure": self.config.failure_url}, "notification_url": self.config.notification_url, "statement_descriptor": self.config.statement_descriptor, "auto_return": "approved"}
        return self._request("POST", "/checkout/preferences", payload=payload)

    def create_pos_checkout_preference(self, *, title: str, amount: float, currency: str, external_reference: str, company_id: int, user_id: int, metadata: dict[str, Any] | None = None, access_token: str | None = None) -> dict[str, Any]:
        payload = {"items": [{"id": external_reference, "title": title, "description": "Cobro QR Mercado Pago desde POS", "quantity": 1, "currency_id": currency, "unit_price": float(amount)}], "external_reference": external_reference, "metadata": {"flow": "pos_sale", "company_id": company_id, "user_id": user_id, **(metadata or {})}, "back_urls": {"success": self.config.success_url, "pending": self.config.pending_url, "failure": self.config.failure_url}, "notification_url": self.config.notification_url, "statement_descriptor": self.config.statement_descriptor, "auto_return": "approved"}
        return self._request("POST", "/checkout/preferences", payload=payload, access_token=access_token)

    @staticmethod
    def _extract_pos_results(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            results = payload.get("results"); return results if isinstance(results, list) else []
        return payload if isinstance(payload, list) else []

    def list_pos_points(self, *, access_token: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/pos?limit={int(limit)}&offset={int(offset)}", access_token=access_token)
        points = []
        for row in self._extract_pos_results(payload):
            if not isinstance(row, dict): continue
            store = row.get("store") if isinstance(row.get("store"), dict) else {}
            points.append({"id": str(row.get("id") or "").strip(), "name": str(row.get("name") or row.get("title") or "POS").strip(), "external_id": str(row.get("external_id") or "").strip(), "store_id": str(row.get("store_id") or store.get("id") or "").strip(), "store_name": str(row.get("store_name") or store.get("name") or "").strip(), "status": str(row.get("status") or "").strip().lower()})
        return points

    def debug_fetch_pos_catalog(self, *, access_token: str | None = None) -> dict[str, Any]:
        path = "/pos?limit=50&offset=0"; req = urlrequest.Request(url=f"{self.API_BASE}{path}", headers=self._headers(access_token=access_token), method="GET"); status_code=None; raw=""
        try:
            with urlrequest.urlopen(req, timeout=self.REQUEST_TIMEOUT_SECONDS) as response: status_code=response.getcode(); raw=response.read().decode("utf-8")
        except HTTPError as exc: status_code=exc.code; raw=exc.read().decode("utf-8", errors="ignore") if hasattr(exc,"read") else str(exc)
        try: payload=json.loads(raw) if raw else {}
        except json.JSONDecodeError: payload={"raw":raw[:1200]}
        return {"path":path,"status_code":status_code,"pos_count":len(self._extract_pos_results(payload)),"response":payload}

    def get_payment(self, payment_id: str) -> dict[str, Any]: return self._request("GET", f"/v1/payments/{payment_id}")
    def get_preapproval(self, preapproval_id: str) -> dict[str, Any]: return self._request("GET", f"/preapproval/{preapproval_id}")
    def get_authorized_payment(self, authorized_payment_id: str) -> dict[str, Any]: return self._request("GET", f"/authorized_payments/{authorized_payment_id}")

    def create_preapproval(self, *, reason: str, payer_email: str, external_reference: str, amount: float, currency: str, frequency: int, frequency_type: str, notification_url: str, back_url: str) -> dict[str, Any]:
        payload = {
            "reason": reason,
            "external_reference": external_reference,
            "payer_email": payer_email,
            "auto_recurring": {
                "frequency": int(frequency),
                "frequency_type": frequency_type,
                "transaction_amount": float(amount),
                "currency_id": currency,
            },
            "back_url": back_url,
            "notification_url": notification_url,
        }
        self._logger().info("Mercado Pago preapproval: company flow, amount=%s currency=%s payer=%s", amount, currency, payer_email)
        return self._request("POST", "/preapproval", payload=payload)

    def update_preapproval(self, preapproval_id: str, payload: dict[str, Any]) -> dict[str, Any]: return self._request("PUT", f"/preapproval/{preapproval_id}", payload=payload)
    def cancel_preapproval(self, preapproval_id: str) -> dict[str, Any]: return self.update_preapproval(preapproval_id, {"status": "canceled"})

    def validate_webhook_signature(self, *, request_id: str, x_signature: str, data_id: str) -> bool:
        secret=(self.config.webhook_secret or "").strip()
        if not secret: return self.config.mode != "production"
        if not x_signature: return False
        parts=dict(part.split("=",1) for part in x_signature.split(",") if "=" in part); ts=parts.get("ts"); v1=parts.get("v1")
        if not ts or not v1 or not request_id: return False
        manifest=f"id:{data_id};request-id:{request_id};ts:{ts};"; digest=hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest,v1)

    @staticmethod
    def parse_webhook_payload(raw_body: bytes) -> dict[str, Any]: return json.loads(raw_body.decode("utf-8")) if raw_body else {}
