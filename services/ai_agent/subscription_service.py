"""Central service for Super Admin management of per-company AI agent subscriptions.

Persists state inside Company.preferences_json["ai_agent"] (reusing the existing
plan_code field from services.ai_agent.config_service) instead of a new table,
per the minimal-footprint design agreed for this feature.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services.ai_agent.config_service import get_ai_preferences, update_ai_preferences
from services.ai_agent.usage_service import AI_PLAN_BY_CODE, usage_snapshot
from stockarmobile.helpers.dates import utcnow_naive


class AISubscriptionError(ValueError):
    pass


VALID_STATUSES = {"TRIAL", "ACTIVA", "PENDIENTE", "VENCIDA", "SUSPENDIDA", "CANCELADA"}
VALID_ORIGINS = {"MANUAL", "MERCADO_PAGO"}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class AISubscriptionService:
    """Super Admin-only operations over a company's AI agent subscription."""

    @staticmethod
    def _ai_prefs(company) -> dict[str, Any]:
        return get_ai_preferences(company)["ai_agent"]

    @classmethod
    def get_status(cls, company) -> dict[str, Any]:
        """Returns the current, freshly-expired-if-needed subscription snapshot for a company."""
        cls.expire_if_needed(company)
        ai = cls._ai_prefs(company)
        plan_code = str(ai.get("plan_code") or "").strip().lower()
        plan = AI_PLAN_BY_CODE.get(plan_code)
        snapshot = usage_snapshot(company.id)
        return {
            "plan_code": plan_code or None,
            "plan_name": plan["name"] if plan else None,
            "plan_limit": plan["limit"] if plan else None,
            "agents": list(plan["agents"]) if plan else [],
            "invoice_processing": bool(plan.get("invoice_processing")) if plan else False,
            "status": str(ai.get("status") or "").strip().upper() or None,
            "starts_at": ai.get("starts_at"),
            "ends_at": ai.get("ends_at"),
            "origin": str(ai.get("origin") or "").strip().upper() or None,
            "granted_by_user_id": ai.get("granted_by_user_id"),
            "trial_reason": ai.get("trial_reason"),
            "mercadopago_preapproval_id": ai.get("mercadopago_preapproval_id"),
            "mercadopago_status": ai.get("mercadopago_status"),
            "mercadopago_external_reference": ai.get("mercadopago_external_reference"),
            "last_payment_id": ai.get("last_payment_id"),
            "last_payment_status": ai.get("last_payment_status"),
            "last_payment_at": ai.get("last_payment_at"),
            "last_payment_amount": ai.get("last_payment_amount"),
            "last_payment_currency": ai.get("last_payment_currency"),
            "usage": snapshot,
        }

    @classmethod
    def expire_if_needed(cls, company) -> bool:
        """Lazily flips TRIAL/ACTIVA -> VENCIDA once ends_at is in the past. Returns True if changed."""
        ai = cls._ai_prefs(company)
        status = str(ai.get("status") or "").strip().upper()
        ends_at = _parse_dt(ai.get("ends_at"))
        if status in {"TRIAL", "ACTIVA"} and ends_at is not None and ends_at < utcnow_naive():
            cls._apply(company, admin_user_id=ai.get("granted_by_user_id"), action="ai_subscription_expire",
                       new_fields={"status": "VENCIDA"}, reason="Vencimiento autom\u00e1tico.")
            return True
        return False

    @classmethod
    def _apply(cls, company, *, admin_user_id, action: str, new_fields: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        from app import AuditLog, db

        before = dict(cls._ai_prefs(company))
        update_ai_preferences(company, ai_updates={**new_fields, "updated_at": utcnow_naive().isoformat()})
        after = dict(cls._ai_prefs(company))
        db.session.add(AuditLog(
            user_id=admin_user_id,
            company_id=company.id,
            action=action,
            entity="company_ai_subscription",
            entity_id=company.id,
            detail=(
                f"plan: {before.get('plan_code')!r} -> {after.get('plan_code')!r}; "
                f"status: {before.get('status')!r} -> {after.get('status')!r}"
                + (f"; motivo: {reason}" if reason else "")
            ),
        ))
        db.session.commit()
        return cls.get_status(company)

    @classmethod
    def assign_plan(cls, company, *, plan_code: str, admin_user_id, origin: str = "MANUAL") -> dict[str, Any]:
        code = str(plan_code or "").strip().lower()
        if code not in AI_PLAN_BY_CODE:
            raise AISubscriptionError("Plan IA inv\u00e1lido.")
        origin_norm = str(origin or "MANUAL").strip().upper()
        if origin_norm not in VALID_ORIGINS:
            raise AISubscriptionError("Origen inv\u00e1lido.")
        current_status = str(cls._ai_prefs(company).get("status") or "").strip().upper()
        new_status = current_status if current_status in VALID_STATUSES else "ACTIVA"
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_assign_plan",
                           new_fields={"plan_code": code, "status": new_status, "origin": origin_norm})

    @classmethod
    def activate(cls, company, *, admin_user_id) -> dict[str, Any]:
        if str(cls._ai_prefs(company).get("status") or "").strip().upper() == "ACTIVA":
            return cls.get_status(company)
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_activate", new_fields={"status": "ACTIVA"})

    @classmethod
    def suspend(cls, company, *, admin_user_id, reason: str | None = None) -> dict[str, Any]:
        if str(cls._ai_prefs(company).get("status") or "").strip().upper() == "SUSPENDIDA":
            return cls.get_status(company)
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_suspend", new_fields={"status": "SUSPENDIDA"}, reason=reason)

    @classmethod
    def reactivate(cls, company, *, admin_user_id) -> dict[str, Any]:
        if str(cls._ai_prefs(company).get("status") or "").strip().upper() == "ACTIVA":
            return cls.get_status(company)
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_reactivate", new_fields={"status": "ACTIVA"})

    @classmethod
    def cancel(cls, company, *, admin_user_id, reason: str | None = None) -> dict[str, Any]:
        if str(cls._ai_prefs(company).get("status") or "").strip().upper() == "CANCELADA":
            return cls.get_status(company)
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_cancel", new_fields={"status": "CANCELADA"}, reason=reason)

    @classmethod
    def grant_trial(cls, company, *, plan_code: str, days: int, admin_user_id, reason: str | None = None) -> dict[str, Any]:
        code = str(plan_code or "").strip().lower()
        if code not in AI_PLAN_BY_CODE:
            raise AISubscriptionError("Plan IA inv\u00e1lido.")
        days_int = max(1, min(int(days or 0), 365))
        now = utcnow_naive()
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_grant_trial", new_fields={
            "plan_code": code,
            "status": "TRIAL",
            "origin": "MANUAL",
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(days=days_int)).isoformat(),
            "granted_by_user_id": admin_user_id,
            "trial_reason": (reason or "").strip()[:300] or None,
        }, reason=reason)

    @classmethod
    def renew(cls, company, *, admin_user_id, days: int = 30) -> dict[str, Any]:
        days_int = max(1, min(int(days or 30), 365))
        now = utcnow_naive()
        current_ends_at = _parse_dt(cls._ai_prefs(company).get("ends_at"))
        base = current_ends_at if current_ends_at and current_ends_at > now else now
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_renew", new_fields={
            "status": "ACTIVA",
            "ends_at": (base + timedelta(days=days_int)).isoformat(),
        })

    @classmethod
    def set_expiry(cls, company, *, ends_at: str, admin_user_id) -> dict[str, Any]:
        parsed = _parse_dt(ends_at)
        if parsed is None:
            raise AISubscriptionError("Fecha de vencimiento inv\u00e1lida.")
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_set_expiry", new_fields={"ends_at": parsed.isoformat()})

    # --- Mercado Pago (origin=MERCADO_PAGO). Reusa AI_PLANS/_apply; no crea un segundo catalogo ni servicio. ---

    @staticmethod
    def plan_amount_ars(plan_code: str) -> float:
        """Resuelve el precio oficial desde AI_PLANS (nunca confiar en un monto enviado por el frontend)."""
        plan = AI_PLAN_BY_CODE.get(str(plan_code or "").strip().lower())
        if plan is None:
            raise AISubscriptionError("Plan IA inv\u00e1lido.")
        digits = "".join(ch for ch in str(plan.get("price") or "").split("/")[0] if ch.isdigit())
        if not digits:
            raise AISubscriptionError("El plan IA no tiene un precio v\u00e1lido configurado.")
        return float(digits)

    @classmethod
    def link_mercadopago_pending(cls, company, *, plan_code: str, preapproval_id: str, payer_email: str, external_reference: str | None = None) -> dict[str, Any]:
        """Registra el preapproval reci\u00e9n creado ANTES de redirigir a Mercado Pago. Queda en PENDIENTE hasta que el webhook confirme."""
        code = str(plan_code or "").strip().lower()
        if code not in AI_PLAN_BY_CODE:
            raise AISubscriptionError("Plan IA inv\u00e1lido.")
        if not preapproval_id:
            raise AISubscriptionError("Mercado Pago no devolvi\u00f3 un identificador de suscripci\u00f3n v\u00e1lido.")
        return cls._apply(company, admin_user_id=None, action="ai_subscription_mercadopago_checkout_created", new_fields={
            "plan_code": code,
            "status": "PENDIENTE",
            "origin": "MERCADO_PAGO",
            "mercadopago_preapproval_id": str(preapproval_id).strip(),
            "mercadopago_payer_email": str(payer_email or "").strip().lower(),
            "mercadopago_external_reference": str(external_reference or "").strip() or None,
            "mercadopago_status": "pending",
        })

    @classmethod
    def company_for_mercadopago_reference(cls, *, preapproval_id: str, external_reference: str):
        from app import Company

        parts = dict(segment.split(":", 1) for segment in str(external_reference or "").split("|") if ":" in segment)
        if parts.get("ai_subscription") != "true":
            return None
        try:
            company_id = int(parts.get("company_id") or 0)
        except (TypeError, ValueError):
            return None
        if not company_id:
            return None
        company = Company.query.get(company_id)
        if company is None:
            return None
        stored_id = str(cls._ai_prefs(company).get("mercadopago_preapproval_id") or "").strip()
        return company if preapproval_id and stored_id == str(preapproval_id).strip() else None

    @classmethod
    def record_mercadopago_payment(cls, company, *, payment_id: str, payment_status: str, amount: float | None, currency: str | None, paid_at: datetime | None, preapproval_id: str, external_reference: str) -> dict[str, Any]:
        fields = {
            "last_payment_id": str(payment_id or "").strip() or None,
            "last_payment_status": str(payment_status or "").strip().lower() or None,
            "last_payment_amount": float(amount or 0) if amount is not None else None,
            "last_payment_currency": str(currency or "ARS").strip().upper() or "ARS",
            "last_payment_at": paid_at.isoformat() if paid_at else utcnow_naive().isoformat(),
            "mercadopago_preapproval_id": str(preapproval_id or "").strip() or None,
            "mercadopago_external_reference": str(external_reference or "").strip() or None,
            "origin": "MERCADO_PAGO",
        }
        return cls._apply(company, admin_user_id=None, action="ai_subscription_payment_recorded", new_fields=fields, reason=f"payment_status={payment_status}")

    @classmethod
    def sync_from_mercadopago(cls, *, preapproval: dict):
        """Procesa un preapproval de Mercado Pago proveniente del webhook. Devuelve la Company afectada o None
        si el preapproval no corresponde a una suscripci\u00f3n IA (o no puede verificarse de forma segura)."""
        from app import Company

        external_reference = str((preapproval or {}).get("external_reference") or "")
        parts = dict(segment.split(":", 1) for segment in external_reference.split("|") if ":" in segment)
        if parts.get("ai_subscription") != "true":
            return None
        try:
            company_id = int(parts.get("company_id") or 0)
        except (TypeError, ValueError):
            return None
        if not company_id:
            return None
        company = Company.query.get(company_id)
        if company is None:
            return None

        preapproval_id = str((preapproval or {}).get("id") or "").strip()
        stored_id = str(cls._ai_prefs(company).get("mercadopago_preapproval_id") or "").strip()
        if not preapproval_id or not stored_id or stored_id != preapproval_id:
            # El company_id embebido en el external_reference NUNCA se acepta solo: debe coincidir con
            # el preapproval_id que nosotros mismos guardamos para esa empresa al crear el checkout.
            return None

        mp_status = str((preapproval or {}).get("status") or "").strip().lower()
        status_map = {
            "authorized": "ACTIVA",
            "pending": "PENDIENTE",
            "paused": "SUSPENDIDA",
            "cancelled": "CANCELADA",
            "canceled": "CANCELADA",
            "expired": "VENCIDA",
        }
        new_status = status_map.get(mp_status)
        if new_status is None:
            return company

        new_fields: dict[str, Any] = {"status": new_status, "origin": "MERCADO_PAGO", "mercadopago_status": mp_status}
        if new_status == "ACTIVA":
            next_payment = (preapproval or {}).get("next_payment_date")
            if next_payment and _parse_dt(next_payment) is not None:
                new_fields["ends_at"] = _parse_dt(next_payment).isoformat()
        cls._apply(company, admin_user_id=None, action="ai_subscription_mercadopago_sync", new_fields=new_fields, reason=f"mercadopago_status={mp_status}")
        return company
