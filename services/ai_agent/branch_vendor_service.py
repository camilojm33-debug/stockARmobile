"""Branch-scoped Vendedor IA licenses and Mercado Pago lifecycle.

The company AI plan remains the entitlement for the first/default branch and
continues to own the shared monthly interaction quota. Additional branches are
unlocked only by a separately paid license.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


PRICE_SETTING_KEY = "branch_vendor_monthly_price_ars"
LICENSE_ACTIVE_STATUSES = {"active"}
TERMINAL_PAYMENT_STATUSES = {"cancelled", "canceled", "refunded", "refund_pending", "charged_back"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_external_reference(value: str | None) -> dict[str, str]:
    parts: dict[str, str] = {}
    for segment in str(value or "").split("|"):
        if ":" in segment:
            key, item = segment.split(":", 1)
            parts[key.strip()] = item.strip()
    return parts


def monthly_price_ars() -> Decimal:
    """Read the global monthly license price configured by SuperAdmin."""
    from app import SaaSSetting

    row = SaaSSetting.query.filter_by(key=PRICE_SETTING_KEY).first()
    raw = row.value if row is not None else os.getenv("AI_BRANCH_VENDOR_PRICE_ARS", "0")
    try:
        price = Decimal(str(raw or "0").replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")
    return price if price > 0 else Decimal("0.00")


def set_monthly_price_ars(value: Any, *, user_id: int | None = None) -> Decimal:
    from app import SaaSSetting, db, utcnow

    try:
        price = Decimal(str(value or "").strip().replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Ingresá un importe mensual válido.") from None
    if price <= 0 or price > Decimal("100000000"):
        raise ValueError("El precio mensual debe ser mayor a cero y menor a $100.000.000.")
    row = SaaSSetting.query.filter_by(key=PRICE_SETTING_KEY).with_for_update().first()
    if row is None:
        row = SaaSSetting(key=PRICE_SETTING_KEY, value=str(price))
        db.session.add(row)
    else:
        row.value = str(price)
    row.updated_at = utcnow()
    row.updated_by_user_id = user_id
    db.session.flush()
    return price


def get_license(company_id: int, branch_id: int, *, lock: bool = False):
    from app import BranchVendorLicense

    query = BranchVendorLicense.query.filter_by(
        company_id=int(company_id), branch_id=int(branch_id)
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def branch_vendor_access(company, branch, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the branch's seller entitlement without consuming AI interactions."""
    from services.ai_agent.usage_service import can_use_ai_feature

    now = now or _now()
    base_access = can_use_ai_feature(company, "vendedor")
    if not base_access.allowed:
        return {
            "allowed": False,
            "included": False,
            "status": "base_plan_required",
            "reason": base_access.reason or "La empresa necesita un plan IA que incluya Vendedor IA.",
            "license": None,
        }
    if branch is None or int(getattr(branch, "company_id", 0) or 0) != int(company.id) or not bool(branch.active):
        return {"allowed": False, "included": False, "status": "branch_unavailable", "reason": "La sucursal no está disponible.", "license": None}
    if bool(getattr(branch, "is_default", False)):
        return {
            "allowed": True,
            "included": True,
            "status": "included",
            "reason": None,
            "license": None,
        }
    license_row = get_license(company.id, branch.id)
    starts_at = _parse_dt(getattr(license_row, "starts_at", None)) if license_row is not None else None
    ends_at = _parse_dt(getattr(license_row, "ends_at", None)) if license_row is not None else None
    active = bool(
        license_row is not None
        and str(license_row.status or "").lower() in LICENSE_ACTIVE_STATUSES
        and starts_at is not None
        and starts_at <= now
        and ends_at is not None
        and ends_at > now
    )
    if active:
        return {"allowed": True, "included": False, "status": "active", "reason": None, "license": license_row}
    status = str(getattr(license_row, "status", "") or "not_contracted").lower()
    if license_row is not None and ends_at is not None and ends_at <= now and status == "active":
        status = "expired"
    return {
        "allowed": False,
        "included": False,
        "status": status,
        "reason": "Esta sucursal necesita una licencia Vendedor IA activa.",
        "license": license_row,
    }


def list_branch_vendor_statuses(company) -> list[dict[str, Any]]:
    from app import Branch

    result = []
    price = monthly_price_ars()
    for branch in Branch.query.filter_by(company_id=company.id, active=True).order_by(Branch.is_default.desc(), Branch.name.asc(), Branch.id.asc()).all():
        access = branch_vendor_access(company, branch)
        license_row = access.get("license")
        result.append({
            "branch": branch,
            "included": bool(access.get("included")),
            "allowed": bool(access.get("allowed")),
            "status": access.get("status"),
            "reason": access.get("reason"),
            "price": price,
            "license": license_row,
            "ends_at": getattr(license_row, "ends_at", None) if license_row else None,
            "payment_mode": getattr(license_row, "payment_mode", None) if license_row else None,
            "auto_renew": bool(getattr(license_row, "auto_renew", False)) if license_row else False,
            "cancel_at_period_end": bool(getattr(license_row, "cancel_at_period_end", False)) if license_row else False,
        })
    return result


def ensure_license(company_id: int, branch_id: int, *, lock: bool = True):
    from app import Branch, BranchVendorLicense, db

    branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company_id), active=True).first()
    if branch is None:
        raise ValueError("La sucursal no existe o está inactiva.")
    if branch.is_default:
        raise ValueError("La sucursal principal ya tiene el Vendedor IA incluido en el plan.")
    row = get_license(company_id, branch_id, lock=lock)
    if row is None:
        row = BranchVendorLicense(
            company_id=int(company_id),
            branch_id=int(branch_id),
            status="not_contracted",
            currency="ARS",
            auto_renew=False,
            cancel_at_period_end=False,
        )
        db.session.add(row)
        db.session.flush()
    return row


def activate_qr_license(*, company_id: int, branch_id: int, license_id: int, payment_id: str, amount: Decimal, currency: str, paid_at: datetime | None = None):
    from app import BranchVendorLicense, Payment, db

    license_row = BranchVendorLicense.query.filter_by(
        id=int(license_id), company_id=int(company_id), branch_id=int(branch_id)
    ).with_for_update().first()
    if license_row is None:
        raise ValueError("No se encontró la licencia de la sucursal para activar.")
    if Decimal(str(license_row.amount or 0)).quantize(Decimal("0.01")) != Decimal(str(amount)).quantize(Decimal("0.01")):
        raise ValueError("El importe del pago QR no coincide con la licencia.")
    if str(currency or "").upper() != "ARS":
        raise ValueError("La licencia adicional debe abonarse en ARS.")
    if str(license_row.last_payment_id or "") == str(payment_id):
        return license_row
    payment = Payment.query.filter_by(payment_id=str(payment_id)).first()
    if payment is not None and int(payment.company_id or 0) != int(company_id):
        raise ValueError("El pago ya está asociado a otra empresa.")
    now = paid_at or _now()
    previous_end = _parse_dt(license_row.ends_at)
    starts = max(now, previous_end) if previous_end and previous_end > now else now
    license_row.status = "active"
    license_row.payment_mode = "qr"
    license_row.starts_at = starts
    license_row.ends_at = starts + timedelta(days=30)
    license_row.preapproval_id = None
    license_row.external_reference = None
    license_row.auto_renew = False
    license_row.cancel_at_period_end = False
    license_row.last_payment_id = str(payment_id)
    license_row.last_payment_at = paid_at or now
    license_row.updated_at = _now()
    db.session.flush()
    return license_row


def sync_preapproval(preapproval: dict[str, Any]):
    """Synchronize a branch-specific monthly preapproval; never touches company AI plan."""
    from app import BranchVendorLicense, Branch, db

    external_reference = str(preapproval.get("external_reference") or "").strip()
    parts = parse_external_reference(external_reference)
    if parts.get("branch_vendor_agent") != "true" or parts.get("flow") != "branch_vendor_agent":
        return None
    try:
        company_id = int(parts.get("company_id") or 0)
        branch_id = int(parts.get("branch_id") or 0)
        license_id = int(parts.get("license_id") or 0)
    except (TypeError, ValueError):
        raise ValueError("La referencia de Mercado Pago para la licencia de sucursal es inválida.") from None
    branch = Branch.query.filter_by(id=branch_id, company_id=company_id, active=True).first()
    license_row = BranchVendorLicense.query.filter_by(id=license_id, company_id=company_id, branch_id=branch_id).with_for_update().first()
    remote_id = str(preapproval.get("id") or "").strip()
    if branch is None or license_row is None or not remote_id:
        raise ValueError("La suscripción mensual no coincide con una licencia de sucursal registrada.")
    if license_row.external_reference and license_row.external_reference != external_reference:
        raise ValueError("La referencia mensual no coincide con la licencia registrada.")
    if license_row.preapproval_id and license_row.preapproval_id != remote_id:
        raise ValueError("La autorización mensual pertenece a otra licencia.")
    status = str(preapproval.get("status") or "").strip().lower()
    auto_recurring = preapproval.get("auto_recurring") if isinstance(preapproval.get("auto_recurring"), dict) else {}
    amount = auto_recurring.get("transaction_amount")
    currency = str(auto_recurring.get("currency_id") or "ARS").upper()
    if status == "authorized":
        try:
            incoming_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Mercado Pago no devolvió un importe mensual válido para la sucursal.") from None
        expected = Decimal(str(license_row.amount or 0)).quantize(Decimal("0.01"))
        if incoming_amount != expected or currency != "ARS":
            raise ValueError("La autorización mensual tiene un importe o una moneda distinta de la licencia.")
        license_row.payment_mode = "automatic"
        license_row.preapproval_id = remote_id
        license_row.auto_renew = True
        if license_row.status not in {"active"} or not _parse_dt(license_row.ends_at) or _parse_dt(license_row.ends_at) <= _now():
            # Access starts only after the first verified authorized charge.
            license_row.status = "pending"
    elif status in {"pending", "in_process"}:
        license_row.payment_mode = "automatic"
        license_row.preapproval_id = remote_id
        license_row.status = "pending"
        license_row.auto_renew = True
    elif status in {"cancelled", "canceled", "expired", "paused"}:
        license_row.auto_renew = False
        license_row.cancel_at_period_end = True
        ends_at = _parse_dt(license_row.ends_at)
        if ends_at is None or ends_at <= _now():
            license_row.status = "cancelled" if status in {"cancelled", "canceled"} else "expired"
    else:
        raise ValueError(f"Estado mensual de Mercado Pago no reconocido: {status or 'vacío'}.")
    license_row.external_reference = external_reference
    license_row.updated_at = _now()
    db.session.flush()
    return license_row


def record_authorized_payment(authorized: dict[str, Any]):
    """Record and credit a recurring branch-agent charge exactly once by MP payment ID."""
    from app import Branch, BranchVendorLicense, Payment, db

    external_reference = str(authorized.get("external_reference") or "").strip()
    parts = parse_external_reference(external_reference)
    if parts.get("branch_vendor_agent") != "true" or parts.get("flow") != "branch_vendor_agent":
        return None
    try:
        company_id = int(parts.get("company_id") or 0)
        branch_id = int(parts.get("branch_id") or 0)
        license_id = int(parts.get("license_id") or 0)
    except (TypeError, ValueError):
        raise ValueError("La referencia del cobro recurrente de sucursal es inválida.") from None
    branch = Branch.query.filter_by(id=branch_id, company_id=company_id, active=True).first()
    license_row = BranchVendorLicense.query.filter_by(id=license_id, company_id=company_id, branch_id=branch_id).with_for_update().first()
    preapproval_id = str(authorized.get("preapproval_id") or "").strip()
    payment_info = authorized.get("payment") if isinstance(authorized.get("payment"), dict) else {}
    payment_id = str(payment_info.get("id") or "").strip()
    if branch is None or license_row is None or not preapproval_id or not payment_id:
        raise ValueError("El cobro mensual no está vinculado a una sucursal y licencia verificables.")
    if license_row.external_reference != external_reference or license_row.preapproval_id != preapproval_id:
        raise ValueError("El cobro mensual no coincide con la autorización registrada de la sucursal.")
    status = str(payment_info.get("status") or authorized.get("summarized") or "pending").strip().lower()
    try:
        amount_value = authorized.get("transaction_amount") or payment_info.get("transaction_amount")
        incoming_amount = Decimal(str(amount_value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        incoming_amount = Decimal("0.00")
    currency = str(authorized.get("currency_id") or payment_info.get("currency_id") or "").strip().upper()
    expected = Decimal(str(license_row.amount or 0)).quantize(Decimal("0.01"))
    if status == "approved" and (incoming_amount != expected or currency != "ARS"):
        raise ValueError("El cobro recurrente de sucursal fue rechazado por importe o moneda incorrectos.")
    previous = Payment.query.filter_by(payment_id=payment_id).first()
    if previous is not None and (int(previous.company_id or 0) != company_id or previous.provider != "mercadopago_branch_vendor_subscription"):
        raise ValueError("El identificador del cobro ya pertenece a otro flujo de pago.")
    if previous is None:
        payment = Payment(
            payment_id=payment_id,
            external_reference=external_reference,
            company_id=company_id,
            subscription_id=None,
            user_id=None,
            amount=incoming_amount,
            currency=currency or "ARS",
            status=status,
            payment_method="mercadopago_branch_vendor_subscription",
            reference=preapproval_id,
            provider="mercadopago_branch_vendor_subscription",
            payload_json=json.dumps(authorized, ensure_ascii=False),
        )
        db.session.add(payment)
    else:
        payment = previous
        payment.status = status
        payment.amount = incoming_amount or payment.amount
        payment.currency = currency or payment.currency or "ARS"
        payment.payload_json = json.dumps(authorized, ensure_ascii=False)
    payment.paid_at = _parse_dt(authorized.get("debit_date")) or payment.paid_at
    if status == "approved" and str(license_row.last_payment_id or "") != payment_id:
        now = payment.paid_at or _now()
        previous_end = _parse_dt(license_row.ends_at)
        starts = max(now, previous_end) if previous_end and previous_end > now else now
        license_row.status = "active"
        license_row.payment_mode = "automatic"
        license_row.starts_at = license_row.starts_at or starts
        license_row.ends_at = starts + timedelta(days=30)
        license_row.last_payment_id = payment_id
        license_row.last_payment_at = now
        license_row.auto_renew = True
        license_row.cancel_at_period_end = False
    elif status in {"refunded", "charged_back"}:
        license_row.status = "suspended"
        license_row.auto_renew = False
        license_row.cancel_at_period_end = True
    license_row.updated_at = _now()
    db.session.flush()
    return {"license": license_row, "payment": payment, "status": status}


def cancel_recurring_license(company_id: int, branch_id: int, *, user_id: int | None = None) -> dict[str, Any]:
    from app import BranchVendorLicense, db
    from services.mercadopago_service import MercadoPagoService

    row = BranchVendorLicense.query.filter_by(
        company_id=int(company_id), branch_id=int(branch_id)
    ).with_for_update().first()
    if row is None or str(row.payment_mode or "").lower() != "automatic" or not row.preapproval_id:
        raise ValueError("Esta sucursal no tiene una suscripción mensual para cancelar.")
    remote = MercadoPagoService().get_preapproval(row.preapproval_id)
    if str(remote.get("id") or "").strip() != row.preapproval_id:
        raise ValueError("Mercado Pago devolvió una autorización distinta de la registrada.")
    status = str(remote.get("status") or "").strip().lower()
    if status not in {"cancelled", "canceled", "expired"}:
        remote = MercadoPagoService().cancel_preapproval(row.preapproval_id)
        if str(remote.get("status") or "").strip().lower() not in {"cancelled", "canceled", "expired"}:
            raise ValueError("Mercado Pago no confirmó la cancelación de la renovación mensual.")
    row.auto_renew = False
    row.cancel_at_period_end = True
    if not _parse_dt(row.ends_at) or _parse_dt(row.ends_at) <= _now():
        row.status = "cancelled"
    row.updated_at = _now()
    db.session.flush()
    ends_at = _parse_dt(row.ends_at)
    return {
        "status": row.status,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "access_preserved": bool(row.status == "active" and ends_at and ends_at > _now()),
    }
