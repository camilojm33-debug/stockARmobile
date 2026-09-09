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
MP_TERMINAL_STATUSES = {"cancelled", "canceled", "expired"}


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
    """Central service for the lifecycle of a company's AI subscription."""

    @staticmethod
    def _ai_prefs(company) -> dict[str, Any]:
        return get_ai_preferences(company)["ai_agent"]

    @staticmethod
    def _mp_service():
        from services.mercadopago_service import MercadoPagoService
        return MercadoPagoService()

    @staticmethod
    def _origin(ai: dict[str, Any]) -> str:
        return str(ai.get("origin") or "").strip().upper()

    @staticmethod
    def _preapproval_id(ai: dict[str, Any]) -> str:
        return str(ai.get("mercadopago_preapproval_id") or "").strip()

    @classmethod
    def _require_manual(cls, company, operation: str) -> None:
        ai = cls._ai_prefs(company)
        if cls._origin(ai) == "MERCADO_PAGO":
            raise AISubscriptionError(
                f"La suscripción IA de Mercado Pago no admite {operation} manualmente. "
                "Debe gestionarse desde su preapproval de Mercado Pago."
            )

    @classmethod
    def _get_mp_preapproval(cls, company) -> dict[str, Any]:
        ai = cls._ai_prefs(company)
        preapproval_id = cls._preapproval_id(ai)
        if cls._origin(ai) != "MERCADO_PAGO" or not preapproval_id:
            raise AISubscriptionError("La suscripción IA no tiene un preapproval de Mercado Pago operativo.")
        try:
            remote = cls._mp_service().get_preapproval(preapproval_id)
        except Exception as exc:
            raise AISubscriptionError("No se pudo consultar el estado de la suscripción IA en Mercado Pago.") from exc
        if not isinstance(remote, dict) or not str(remote.get("id") or "").strip():
            raise AISubscriptionError("Mercado Pago devolvió una suscripción IA inválida.")
        if str(remote.get("id")).strip() != preapproval_id:
            raise AISubscriptionError("La suscripción IA de Mercado Pago no coincide con la registrada en la empresa.")
        return remote

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
        """Lazily flips only locally-managed TRIAL/ACTIVA subscriptions to VENCIDA."""
        ai = cls._ai_prefs(company)
        status = str(ai.get("status") or "").strip().upper()
        ends_at = _parse_dt(ai.get("ends_at"))
        if cls._origin(ai) == "MERCADO_PAGO":
            return False
        if status in {"TRIAL", "ACTIVA"} and ends_at is not None and ends_at < utcnow_naive():
            cls._apply(
                company,
                admin_user_id=ai.get("granted_by_user_id"),
                action="ai_subscription_expire",
                new_fields={"status": "VENCIDA"},
                reason="Vencimiento automático.",
            )
            return True
        return False

    @classmethod
    def _apply(
        cls,
        company,
        *,
        admin_user_id,
        action: str,
        new_fields: dict[str, Any],
        reason: str | None = None,
    ) -> dict[str, Any]:
        from app import AuditLog, db

        before = dict(cls._ai_prefs(company))
        update_ai_preferences(company, ai_updates={**new_fields, "updated_at": utcnow_naive().isoformat()})
        after = dict(cls._ai_prefs(company))
        db.session.add(
            AuditLog(
                user_id=admin_user_id,
                company_id=company.id,
                action=action,
                entity="company_ai_subscription",
                entity_id=company.id,
                detail=(
                    f"plan: {before.get('plan_code')!r} -> {after.get('plan_code')!r}; "
                    f"status: {before.get('status')!r} -> {after.get('status')!r}; "
                    f"origin: {before.get('origin')!r} -> {after.get('origin')!r}"
                    + (f"; motivo: {reason}" if reason else "")
                ),
            )
        )
        db.session.commit()
        return cls.get_status(company)

    @classmethod
    def assign_plan(cls, company, *, plan_code: str, admin_user_id, origin: str = "MANUAL") -> dict[str, Any]:
        code = str(plan_code or "").strip().lower()
        if code not in AI_PLAN_BY_CODE:
            raise AISubscriptionError("Plan IA inválido.")
        origin_norm = str(origin or "MANUAL").strip().upper()
        if origin_norm not in VALID_ORIGINS:
            raise AISubscriptionError("Origen inválido.")

        ai = cls._ai_prefs(company)
        current_origin = cls._origin(ai)
        current_status = str(ai.get("status") or "").strip().upper()
        current_plan_code = str(ai.get("plan_code") or "").strip().lower()
        preapproval_id = cls._preapproval_id(ai)

        if current_origin == "MERCADO_PAGO":
            if current_status in {"CANCELADA", "VENCIDA"} or not preapproval_id:
                raise AISubscriptionError(
                    "La suscripción IA de Mercado Pago está terminada. Para contratar otro plan debe iniciar un nuevo checkout."
                )
            if current_plan_code == code:
                return cls.get_status(company)

            amount = cls.plan_amount_ars(code)
            remote = cls._get_mp_preapproval(company)
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status in MP_TERMINAL_STATUSES:
                raise AISubscriptionError("Mercado Pago ya no tiene un preapproval IA operativo. Iniciá un nuevo checkout.")

            payload = {
                "reason": f"StockArMobile IA - Plan {AI_PLAN_BY_CODE[code]['name']}",
                "auto_recurring": {
                    "frequency": 1,
                    "frequency_type": "months",
                    "transaction_amount": amount,
                    "currency_id": "ARS",
                },
            }
            try:
                updated = cls._mp_service().update_preapproval(preapproval_id, payload)
            except Exception as exc:
                raise AISubscriptionError("No se pudo actualizar el monto de la suscripción IA en Mercado Pago.") from exc

            updated_id = str(updated.get("id") or "").strip()
            if updated_id != preapproval_id:
                raise AISubscriptionError("Mercado Pago devolvió un preapproval IA diferente al registrado.")
            new_remote_status = str(updated.get("status") or remote_status).strip().lower()
            if new_remote_status in MP_TERMINAL_STATUSES:
                raise AISubscriptionError("Mercado Pago dejó de tener una suscripción IA operativa al cambiar el plan.")

            return cls._apply(
                company,
                admin_user_id=admin_user_id,
                action="ai_subscription_plan_changed",
                new_fields={
                    "plan_code": code,
                    "status": current_status if current_status in VALID_STATUSES else "ACTIVA",
                    "origin": "MERCADO_PAGO",
                    "mercadopago_status": new_remote_status or "authorized",
                },
                reason=f"Cambio de plan IA a {code}; preapproval={preapproval_id}",
            )

        # Una operación manual nunca debe convertir una suscripción MP existente en MANUAL.
        if current_origin == "MERCADO_PAGO":
            raise AISubscriptionError("No se puede convertir una suscripción IA de Mercado Pago en manual desde esta acción.")

        current_origin = "MANUAL"
        new_status = current_status if current_status in VALID_STATUSES else "ACTIVA"
        return cls._apply(
            company,
            admin_user_id=admin_user_id,
            action="ai_subscription_assign_plan",
            new_fields={"plan_code": code, "status": new_status, "origin": current_origin},
        )

    @classmethod
    def activate(cls, company, *, admin_user_id) -> dict[str, Any]:
        ai = cls._ai_prefs(company)
        status = str(ai.get("status") or "").strip().upper()
        if status == "ACTIVA":
            return cls.get_status(company)

        if cls._origin(ai) == "MERCADO_PAGO":
            remote = cls._get_mp_preapproval(company)
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status == "authorized":
                return cls._apply(
                    company,
                    admin_user_id=admin_user_id,
                    action="ai_subscription_activate_sync",
                    new_fields={"status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_status": remote_status},
                    reason="Activación confirmada por Mercado Pago.",
                )
            if remote_status == "paused":
                try:
                    updated = cls._mp_service().update_preapproval(cls._preapproval_id(ai), {"status": "authorized"})
                except Exception as exc:
                    raise AISubscriptionError("No se pudo reactivar la suscripción IA en Mercado Pago.") from exc
                if str(updated.get("status") or "").strip().lower() != "authorized":
                    raise AISubscriptionError("Mercado Pago no confirmó la reactivación de la suscripción IA.")
                return cls._apply(
                    company,
                    admin_user_id=admin_user_id,
                    action="ai_subscription_reactivate_sync",
                    new_fields={"status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_status": "authorized"},
                    reason="Reactivación confirmada por Mercado Pago.",
                )
            if remote_status in MP_TERMINAL_STATUSES:
                raise AISubscriptionError("La suscripción IA de Mercado Pago está terminada. Debe iniciarse un nuevo checkout.")
            raise AISubscriptionError("Mercado Pago todavía no autorizó la suscripción IA. No se puede activar manualmente.")

        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_activate", new_fields={"status": "ACTIVA", "origin": "MANUAL"})

    @classmethod
    def suspend(cls, company, *, admin_user_id, reason: str | None = None) -> dict[str, Any]:
        ai = cls._ai_prefs(company)
        if cls._origin(ai) == "MERCADO_PAGO":
            remote = cls._get_mp_preapproval(company)
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status in MP_TERMINAL_STATUSES:
                raise AISubscriptionError("No se puede suspender una suscripción IA de Mercado Pago que ya terminó.")
            if remote_status != "paused":
                try:
                    updated = cls._mp_service().update_preapproval(cls._preapproval_id(ai), {"status": "paused"})
                except Exception as exc:
                    raise AISubscriptionError("No se pudo pausar la suscripción IA en Mercado Pago.") from exc
                if str(updated.get("status") or "").strip().lower() != "paused":
                    raise AISubscriptionError("Mercado Pago no confirmó la suspensión de la suscripción IA.")
            return cls._apply(
                company,
                admin_user_id=admin_user_id,
                action="ai_subscription_suspend_sync",
                new_fields={"status": "SUSPENDIDA", "origin": "MERCADO_PAGO", "mercadopago_status": "paused"},
                reason=reason,
            )

        if str(ai.get("status") or "").strip().upper() == "SUSPENDIDA":
            return cls.get_status(company)
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_suspend", new_fields={"status": "SUSPENDIDA", "origin": "MANUAL"}, reason=reason)

    @classmethod
    def reactivate(cls, company, *, admin_user_id) -> dict[str, Any]:
        ai = cls._ai_prefs(company)
        if cls._origin(ai) == "MERCADO_PAGO":
            remote = cls._get_mp_preapproval(company)
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status == "authorized":
                return cls._apply(
                    company,
                    admin_user_id=admin_user_id,
                    action="ai_subscription_reactivate_sync",
                    new_fields={"status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_status": "authorized"},
                )
            if remote_status != "paused":
                raise AISubscriptionError("Solo se puede reactivar una suscripción IA de Mercado Pago que esté pausada.")
            try:
                updated = cls._mp_service().update_preapproval(cls._preapproval_id(ai), {"status": "authorized"})
            except Exception as exc:
                raise AISubscriptionError("No se pudo reactivar la suscripción IA en Mercado Pago.") from exc
            if str(updated.get("status") or "").strip().lower() != "authorized":
                raise AISubscriptionError("Mercado Pago no confirmó la reactivación de la suscripción IA.")
            return cls._apply(
                company,
                admin_user_id=admin_user_id,
                action="ai_subscription_reactivate_sync",
                new_fields={"status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_status": "authorized"},
            )

        if str(ai.get("status") or "").strip().upper() == "ACTIVA":
            return cls.get_status(company)
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_reactivate", new_fields={"status": "ACTIVA", "origin": "MANUAL"})

    @classmethod
    def cancel(cls, company, *, admin_user_id, reason: str | None = None) -> dict[str, Any]:
        ai = cls._ai_prefs(company)
        status = str(ai.get("status") or "").strip().upper()
        if status == "CANCELADA":
            return cls.get_status(company)

        if cls._origin(ai) == "MERCADO_PAGO":
            remote = cls._get_mp_preapproval(company)
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status not in MP_TERMINAL_STATUSES:
                try:
                    updated = cls._mp_service().cancel_preapproval(cls._preapproval_id(ai))
                except Exception as exc:
                    raise AISubscriptionError("No se pudo cancelar la suscripción IA en Mercado Pago.") from exc
                if str(updated.get("status") or "").strip().lower() not in MP_TERMINAL_STATUSES:
                    raise AISubscriptionError("Mercado Pago no confirmó la cancelación de la suscripción IA.")
            return cls._apply(
                company,
                admin_user_id=admin_user_id,
                action="ai_subscription_cancel_sync",
                new_fields={"status": "CANCELADA", "origin": "MERCADO_PAGO", "mercadopago_status": "cancelled"},
                reason=reason,
            )

        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_cancel", new_fields={"status": "CANCELADA", "origin": "MANUAL"}, reason=reason)

    @classmethod
    def grant_trial(cls, company, *, plan_code: str, days: int, admin_user_id, reason: str | None = None) -> dict[str, Any]:
        cls._require_manual(company, "otorgar un trial")
        code = str(plan_code or "").strip().lower()
        if code not in AI_PLAN_BY_CODE:
            raise AISubscriptionError("Plan IA inválido.")
        days_int = max(1, min(int(days or 0), 365))
        now = utcnow_naive()
        return cls._apply(
            company,
            admin_user_id=admin_user_id,
            action="ai_subscription_grant_trial",
            new_fields={
                "plan_code": code,
                "status": "TRIAL",
                "origin": "MANUAL",
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(days=days_int)).isoformat(),
                "granted_by_user_id": admin_user_id,
                "trial_reason": (reason or "").strip()[:300] or None,
            },
            reason=reason,
        )

    @classmethod
    def renew(cls, company, *, admin_user_id, days: int = 30) -> dict[str, Any]:
        cls._require_manual(company, "renovar")
        days_int = max(1, min(int(days or 30), 365))
        now = utcnow_naive()
        current_ends_at = _parse_dt(cls._ai_prefs(company).get("ends_at"))
        base = current_ends_at if current_ends_at and current_ends_at > now else now
        return cls._apply(
            company,
            admin_user_id=admin_user_id,
            action="ai_subscription_renew",
            new_fields={"status": "ACTIVA", "origin": "MANUAL", "ends_at": (base + timedelta(days=days_int)).isoformat()},
        )

    @classmethod
    def set_expiry(cls, company, *, ends_at: str, admin_user_id) -> dict[str, Any]:
        cls._require_manual(company, "cambiar vencimiento")
        parsed = _parse_dt(ends_at)
        if parsed is None:
            raise AISubscriptionError("Fecha de vencimiento inválida.")
        return cls._apply(company, admin_user_id=admin_user_id, action="ai_subscription_set_expiry", new_fields={"ends_at": parsed.isoformat(), "origin": "MANUAL"})

    # --- Mercado Pago (origin=MERCADO_PAGO). Reusa AI_PLANS/_apply; no crea un segundo catalogo ni servicio. ---

    @staticmethod
    def plan_amount_ars(plan_code: str) -> float:
        """Resuelve el precio oficial desde AI_PLANS (nunca confiar en un monto enviado por el frontend)."""
        plan = AI_PLAN_BY_CODE.get(str(plan_code or "").strip().lower())
        if plan is None:
            raise AISubscriptionError("Plan IA inválido.")
        digits = "".join(ch for ch in str(plan.get("price") or "").split("/")[0] if ch.isdigit())
        if not digits:
            raise AISubscriptionError("El plan IA no tiene un precio válido configurado.")
        return float(digits)

    @classmethod
    def link_mercadopago_pending(cls, company, *, plan_code: str, preapproval_id: str, payer_email: str, external_reference: str | None = None) -> dict[str, Any]:
        """Registra el preapproval recién creado ANTES de redirigir a Mercado Pago. Queda en PENDIENTE hasta que el webhook confirme."""
        code = str(plan_code or "").strip().lower()
        if code not in AI_PLAN_BY_CODE:
            raise AISubscriptionError("Plan IA inválido.")
        if not preapproval_id:
            raise AISubscriptionError("Mercado Pago no devolvió un identificador de suscripción válido.")
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
        si el preapproval no corresponde a una suscripción IA (o no puede verificarse de forma segura)."""
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
