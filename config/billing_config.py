"""Configuracion de billing y Mercado Pago para Sandbox/Produccion."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BillingConfig:
    mode: str
    access_token: str
    public_key: str
    webhook_secret: str
    notification_url: str
    success_url: str
    pending_url: str
    failure_url: str
    app_url: str
    statement_descriptor: str
    marketplace_fee_percent: float

    @property
    def is_sandbox(self) -> bool:
        return self.mode == "sandbox"



def load_billing_config() -> BillingConfig:
    mode = (os.environ.get("MP_MODE") or "sandbox").strip().lower()
    if mode not in {"sandbox", "production"}:
        mode = "sandbox"

    webhook_secret = (os.environ.get("WEBHOOK_SECRET") or os.environ.get("MP_WEBHOOK_SECRET") or "").strip()
    if mode == "production" and not webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET es obligatorio en produccion.")

    app_url = (os.environ.get("APP_URL") or "http://localhost:5000").rstrip("/")
    notification_url = os.environ.get("MP_NOTIFICATION_URL") or f"{app_url}/admin/webhooks/mercadopago"
    success_url = os.environ.get("MP_SUCCESS_URL") or f"{app_url}/admin/portal?checkout=success"
    pending_url = os.environ.get("MP_PENDING_URL") or f"{app_url}/admin/portal?checkout=pending"
    failure_url = os.environ.get("MP_FAILURE_URL") or f"{app_url}/admin/portal?checkout=failure"

    return BillingConfig(
        mode=mode,
        access_token=(os.environ.get("MP_ACCESS_TOKEN") or "").strip(),
        public_key=(os.environ.get("MP_PUBLIC_KEY") or "").strip(),
        webhook_secret=webhook_secret,
        notification_url=notification_url,
        success_url=success_url,
        pending_url=pending_url,
        failure_url=failure_url,
        app_url=app_url,
        statement_descriptor=(os.environ.get("MP_STATEMENT_DESCRIPTOR") or "STOCKARMOBILE").strip()[:13],
        marketplace_fee_percent=float(os.environ.get("MP_MARKETPLACE_FEE_PERCENT") or 0),
    )
