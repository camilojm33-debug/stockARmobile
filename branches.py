"""Premium multi-branch management routes."""
import re
from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user
from sqlalchemy import func
from app import Branch, Company, User, UserBranchAccess, db, tenant_required, company_admin_required
from stockarmobile.branch_context import SESSION_KEY

bp = Blueprint("branches", __name__, url_prefix="/sucursales")

def _company_id():
    return int(getattr(current_user, "company_id", 0) or 0)


def _lock_company_for_branch_limit(company_id):
    """Serialize branch creation/reactivation for a company on databases with row locks."""
    return (
        Company.query.filter_by(id=company_id, active=True)
        .with_for_update()
        .populate_existing()
        .first_or_404()
    )


def _has_pending_invoice_sales(company_id, branch_id):
    from app import Sale

    cancelled_statuses = (
        "anulada", "anulado", "cancelada", "cancelado",
        "rechazada", "cancelled", "canceled", "void", "voided",
    )
    return (
        Sale.query.filter_by(
            company_id=company_id,
            branch_id=branch_id,
            requiere_comprobante=True,
            comprobante_emitido=False,
        )
        .filter(func.lower(func.coalesce(Sale.status, "")).notin_(cancelled_statuses))
        .first()
        is not None
    )


def _require_company_pin(company):
    from company_billing import _pin_guard
    return _pin_guard(company)


def _require_feature(company):
    from services.ai_agent.usage_service import can_use_commercial_feature
    result = can_use_commercial_feature(company, "multi_branch")
    if not result.allowed:
        flash(result.reason or "La gestión multisucursal está disponible desde el plan Premium.", "warning")
        return redirect(url_for("company_billing.subscription_portal"))
    return None

@bp.route("/seleccionar", methods=["POST"])
@tenant_required
def select_branch():
    company = Company.query.filter_by(id=_company_id(), active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    branch = Branch.query.filter_by(id=request.form.get("branch_id", type=int), company_id=company.id, active=True).first()
    if branch is None:
        abort(404)
    if getattr(current_user, "role", None) not in {"admin", "superadmin"}:
        if UserBranchAccess.query.filter_by(user_id=current_user.id, branch_id=branch.id, company_id=company.id).first() is None:
            abort(403)
    session[SESSION_KEY] = int(branch.id)
    session.modified = True
    flash(f"Sucursal activa: {branch.name}.", "success")
    return redirect(url_for("dashboard.index"))

@bp.route("/crear", methods=["POST"])
@company_admin_required
def create_branch():
    company = Company.query.filter_by(id=_company_id(), active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    pin_blocked = _require_company_pin(company)
    if pin_blocked:
        return pin_blocked
    company = _lock_company_for_branch_limit(company.id)
    from services.plan_usage_service import PlanUsageService
    _subscription, plan = PlanUsageService._active_plan_for_company(company.id)
    limit = max(1, int(getattr(plan, "max_branches", 1) or 1))
    if Branch.query.filter_by(company_id=company.id, active=True).count() >= limit:
        flash(f"Tu plan permite hasta {limit} sucursales. Contactá a administración para ampliar el límite.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    name = (request.form.get("name") or "").strip()[:120]
    if len(name) < 2:
        flash("Ingresá un nombre de sucursal válido.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    raw_code = (request.form.get("code") or name).strip().upper()
    code = re.sub(r"[^A-Z0-9]+", "-", raw_code).strip("-")[:40] or "SUCURSAL"
    if Branch.query.filter_by(company_id=company.id, code=code).first():
        flash("Ya existe una sucursal con ese código.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    branch = Branch(company_id=company.id, name=name, code=code,
        address=(request.form.get("address") or "").strip()[:255] or None,
        city=(request.form.get("city") or "").strip()[:120] or None,
        province=(request.form.get("province") or "").strip()[:120] or None,
        phone=(request.form.get("phone") or "").strip()[:40] or None,
        whatsapp=(request.form.get("whatsapp") or "").strip()[:40] or None,
        active=True, is_default=False)
    try:
        db.session.add(branch)
        db.session.flush()
        created_branch_id = int(branch.id)
        db.session.commit()
        session[SESSION_KEY] = created_branch_id
        session.modified = True
        flash(f"Sucursal {branch.name} creada. Asigná el personal que tendrá acceso.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo crear sucursal para company_id=%s", company.id)
        flash("No se pudo crear la sucursal. No se guardaron cambios.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="branches"))

@bp.route("/<int:branch_id>/actualizar", methods=["POST"])
@company_admin_required
def update_branch(branch_id):
    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    pin_blocked = _require_company_pin(company)
    if pin_blocked:
        return pin_blocked
    company = _lock_company_for_branch_limit(company_id)
    branch = Branch.query.filter_by(id=branch_id, company_id=company_id).first_or_404()
    name = (request.form.get("name") or "").strip()[:120]
    if len(name) < 2:
        flash("El nombre de la sucursal es obligatorio.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    active = request.form.get("active") == "1"
    if not active and (branch.is_default or Branch.query.filter_by(company_id=company_id, active=True).count() <= 1):
        flash("La empresa debe conservar activa su Casa central y al menos una sucursal.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    if active and not branch.active:
        from services.plan_usage_service import PlanUsageService
        _subscription, plan = PlanUsageService._active_plan_for_company(company.id)
        branch_limit = max(1, int(getattr(plan, "max_branches", 1) or 1))
        active_count = Branch.query.filter_by(company_id=company_id, active=True).count()
        if active_count >= branch_limit:
            flash(f"Tu plan permite hasta {branch_limit} sucursales activas.", "warning")
            return redirect(url_for("company_billing.company_settings", panel="branches"))
    if not active:
        from app import CashSession
        if CashSession.query.filter_by(branch_id=branch.id, status="abierta").first() is not None:
            flash("Cerrá las cajas abiertas de esta sucursal antes de desactivarla.", "warning")
            return redirect(url_for("company_billing.company_settings", panel="branches"))
        if _has_pending_invoice_sales(company_id, branch.id):
            flash(
                "Emití los comprobantes pendientes o anulá esas ventas antes de desactivar la sucursal.",
                "warning",
            )
            return redirect(url_for("company_billing.company_settings", panel="branches"))
    branch.name = name
    branch.address = (request.form.get("address") or "").strip()[:255] or None
    branch.city = (request.form.get("city") or "").strip()[:120] or None
    branch.province = (request.form.get("province") or "").strip()[:120] or None
    branch.phone = (request.form.get("phone") or "").strip()[:40] or None
    branch.whatsapp = (request.form.get("whatsapp") or "").strip()[:40] or None
    branch.active = active
    db.session.commit()
    flash("Sucursal actualizada.", "success")
    return redirect(url_for("company_billing.company_settings", panel="branches"))

@bp.route("/transferir-stock", methods=["POST"])
@company_admin_required
def transfer_stock():
    from decimal import Decimal, InvalidOperation
    from app import BranchStockTransfer, Product
    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    pin_blocked = _require_company_pin(company)
    if pin_blocked:
        return pin_blocked
    source_id = request.form.get("source_branch_id", type=int)
    target_id = request.form.get("target_branch_id", type=int)
    product_reference = (request.form.get("product_code") or request.form.get("product_id") or "").strip()
    try:
        quantity = Decimal((request.form.get("quantity") or "0").replace(",", "."))
    except (InvalidOperation, AttributeError):
        quantity = Decimal("0")
    source = Branch.query.filter_by(id=source_id, company_id=company_id, active=True).first()
    target = Branch.query.filter_by(id=target_id, company_id=company_id, active=True).first()
    product_query = Product.query.filter_by(company_id=company_id, active=True)
    product = product_query.filter_by(barcode=product_reference).first() if product_reference else None
    if product is None and product_reference.isdigit():
        product = Product.query.filter_by(id=int(product_reference), company_id=company_id, active=True).first()
    if source is None or target is None or product is None:
        flash("Revisá origen, destino y producto de la transferencia.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    try:
        from stockarmobile.branch_inventory import transfer_branch_stock
        transfer = transfer_branch_stock(
            product, source.id, target.id, quantity,
            user_id=current_user.id, note=request.form.get("note"),
        )
        db.session.commit()
        flash(f"Transferencia #{transfer.id}: {quantity} {product.unit_measure or 'u'} de {source.name} a {target.name}.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Branch stock transfer failed company_id=%s", company_id)
        flash("No se pudo transferir el stock. No se guardaron cambios.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="branches"))


@bp.route("/<int:branch_id>/usuarios", methods=["POST"])
@company_admin_required
def assign_users(branch_id):
    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    pin_blocked = _require_company_pin(company)
    if pin_blocked:
        return pin_blocked
    branch = Branch.query.filter_by(id=branch_id, company_id=company_id, active=True).first_or_404()
    selected_ids = {int(value) for value in request.form.getlist("user_ids") if str(value).isdigit()}
    from stockarmobile.company_memberships import get_company_users
    eligible_users = get_company_users(company_id, active_only=True)
    eligible_by_id = {int(user.id): user for user in eligible_users if user.role != "superadmin"}
    if not selected_ids.issubset(set(eligible_by_id)):
        abort(400)
    users = [eligible_by_id[user_id] for user_id in sorted(selected_ids)]
    for row in UserBranchAccess.query.filter_by(branch_id=branch.id, company_id=company_id).all():
        db.session.delete(row)
    for user in users:
        db.session.add(UserBranchAccess(company_id=company_id, user_id=user.id, branch_id=branch.id))
    db.session.commit()
    flash(f"Accesos de {branch.name} actualizados.", "success")
    return redirect(url_for("company_billing.company_settings", panel="branches"))
