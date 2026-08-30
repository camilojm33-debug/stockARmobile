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
        return current_app.logger if has_app_context() else logging.getLogger(__name__)

    def _headers(self, *, include_idempotency=False, idempotency_key=None, access_token=None):
        token = (access_token or self.config.access_token or "").strip()
        if not token:
            raise RuntimeError("MP_ACCESS_TOKEN no configurado")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "StockArMobile/1.0"}
        if include_idempotency:
            headers["X-Idempotency-Key"] = (idempotency_key or str(uuid.uuid4())).strip()
        return headers

    def _request(self, method, path, *, payload=None, access_token=None, idempotency_key=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urlrequest.Request(url=f"{self.API_BASE}{path}", data=body, headers=self._headers(include_idempotency=method in {"POST", "PUT", "PATCH"}, idempotency_key=idempotency_key, access_token=access_token), method=method)
        try:
            with urlrequest.urlopen(req, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
                status_code = response.getcode()
        except HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else str(exc)
            raise RuntimeError(f"Mercado Pago rechazó la operación (HTTP {exc.code}). {raw_error[:700]}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"Mercado Pago no respondió dentro de {self.REQUEST_TIMEOUT_SECONDS} segundos.") from exc
        if status_code >= 400:
            raise RuntimeError(f"Mercado Pago respondió HTTP {status_code}: {raw[:700]}")
        return json.loads(raw) if raw else {}

    def create_checkout_preference(self, *, title, amount, currency, external_reference, company_id, plan_id, subscription_id, user_id):
        payload = {"items": [{"id": str(plan_id), "title": title, "description": f"Suscripcion plan {title}", "quantity": 1, "currency_id": currency, "unit_price": float(amount)}], "external_reference": external_reference, "metadata": {"company_id": company_id, "plan_id": plan_id, "subscription_id": subscription_id, "user_id": user_id}, "back_urls": {"success": self.config.success_url, "pending": self.config.pending_url, "failure": self.config.failure_url}, "notification_url": self.config.notification_url, "statement_descriptor": self.config.statement_descriptor, "auto_return": "approved"}
        return self._request("POST", "/checkout/preferences", payload=payload, idempotency_key=f"checkout-preference:{external_reference}")

    def create_pos_checkout_preference(self, *, title, amount, currency, external_reference, company_id, user_id, metadata=None, access_token=None):
        payload = {"items": [{"id": external_reference, "title": title, "description": "Cobro QR Mercado Pago desde POS", "quantity": 1, "currency_id": currency, "unit_price": float(amount)}], "external_reference": external_reference, "metadata": {"flow": "pos_sale", "company_id": company_id, "user_id": user_id, **(metadata or {})}, "back_urls": {"success": self.config.success_url, "pending": self.config.pending_url, "failure": self.config.failure_url}, "notification_url": self.config.notification_url, "statement_descriptor": self.config.statement_descriptor, "auto_return": "approved"}
        return self._request("POST", "/checkout/preferences", payload=payload, access_token=access_token, idempotency_key=f"pos-checkout-preference:{external_reference}")

    def create_ai_order_checkout_preference(self, *, title, items, amount, currency, external_reference, company_id, user_id, quote_id, conversation_id, return_url, access_token=None):
        """Create a tenant-scoped Checkout Pro preference for an AI order."""
        if amount <= 0 or not items:
            raise ValueError("El pedido debe contener productos y un importe mayor a cero.")
        safe_items = []
        for item in items:
            quantity = float(item.get("quantity") or 0)
            unit_price = float(item.get("unit_price") or 0)
            if quantity <= 0 or unit_price < 0:
                raise ValueError("Línea de pedido inválida.")
            safe_items.append({"id": str(item.get("id") or ""), "title": str(item.get("title") or "Producto")[:256], "description": str(item.get("description") or item.get("title") or "Producto")[:256], "quantity": int(quantity) if quantity.is_integer() else quantity, "currency_id": str(item.get("currency_id") or currency).upper(), "unit_price": unit_price})
        payload = {"items": safe_items, "external_reference": external_reference, "metadata": {"flow": "ai_order", "company_id": int(company_id), "user_id": int(user_id), "quote_id": int(quote_id), "conversation_id": int(conversation_id)}, "back_urls": {"success": return_url, "pending": return_url, "failure": return_url}, "notification_url": self.config.notification_url, "statement_descriptor": self.config.statement_descriptor, "auto_return": "approved"}
        return self._request("POST", "/checkout/preferences", payload=payload, access_token=access_token, idempotency_key=f"ai-order-preference:{external_reference}")

    def get_payment(self, payment_id): return self._request("GET", f"/v1/payments/{payment_id}")
    def get_preapproval(self, preapproval_id): return self._request("GET", f"/preapproval/{preapproval_id}")
    def get_authorized_payment(self, authorized_payment_id): return self._request("GET", f"/authorized_payments/{authorized_payment_id}")

    def validate_webhook_signature(self, *, request_id, x_signature, data_id):
        secret = (self.config.webhook_secret or "").strip()
        if not secret: return self.config.mode != "production"
        if not x_signature or not request_id: return False
        parts = dict(part.split("=", 1) for part in x_signature.split(",") if "=" in part)
        ts, v1 = parts.get("ts"), parts.get("v1")
        if not ts or not v1: return False
        manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
        digest = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, v1)
