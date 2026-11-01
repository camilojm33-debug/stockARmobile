"""Premium multi-branch management routes."""
import re
from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user
from app import Branch, Company, User, UserBranchAccess, db, tenant_required, company_admin_required
from stockarmobile.branch_context import SESSION_KEY

bp = Blueprint("branches", __name__, url_prefix="/sucursales")

def _company_id():
    return int(getattr(current_user, "company_id", 0) or 0)

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
    return redirect(request.referrer or url_for("dashboard.index"))

@bp.route("/crear", methods=["POST"])
@company_admin_required
def create_branch():
    company = Company.query.filter_by(id=_company_id(), active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
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
        active=True, is_default=False)
    try:
        db.session.add(branch)
        db.session.flush()
        session[SESSION_KEY] = int(branch.id)
        db.session.commit()
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
    branch = Branch.query.filter_by(id=branch_id, company_id=company_id).first_or_404()
    name = (request.form.get("name") or "").strip()[:120]
    if len(name) < 2:
        flash("El nombre de la sucursal es obligatorio.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    active = request.form.get("active") == "1"
    if not active and (branch.is_default or Branch.query.filter_by(company_id=company_id, active=True).count() <= 1):
        flash("La empresa debe conservar activa su Casa central y al menos una sucursal.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    branch.name = name
    branch.address = (request.form.get("address") or "").strip()[:255] or None
    branch.city = (request.form.get("city") or "").strip()[:120] or None
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
    source_id = request.form.get("source_branch_id", type=int)
    target_id = request.form.get("target_branch_id", type=int)
    product_id = request.form.get("product_id", type=int)
    try:
        quantity = Decimal((request.form.get("quantity") or "0").replace(",", "."))
    except (InvalidOperation, AttributeError):
        quantity = Decimal("0")
    source = Branch.query.filter_by(id=source_id, company_id=company_id, active=True).first()
    target = Branch.query.filter_by(id=target_id, company_id=company_id, active=True).first()
    product = Product.query.filter_by(id=product_id, company_id=company_id, active=True).first()
    if source is None or target is None or product is None or source.id == target.id or not quantity.is_finite() or quantity <= 0:
        flash("Revisá origen, destino, producto y cantidad de la transferencia.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    try:
        from stockarmobile.branch_inventory import adjust_branch_stock, get_branch_stock
        source_stock = get_branch_stock(product, source.id, lock=True, create=True)
        if Decimal(str(source_stock.stock or 0)) < quantity:
            raise ValueError(f"Stock insuficiente en {source.name}. Disponible: {source_stock.stock}.")
        transfer = BranchStockTransfer(
            company_id=company_id, source_branch_id=source.id, target_branch_id=target.id,
            product_id=product.id, quantity=quantity,
            note=(request.form.get("note") or "").strip()[:255] or None,
            created_by_user_id=current_user.id,
        )
        db.session.add(transfer)
        db.session.flush()
        adjust_branch_stock(product, source.id, -quantity, movement_type="transfer_out",
            source_type="branch_transfer", source_id=transfer.id, user_id=current_user.id,
            update_company_total=False, note=f"Transferencia hacia {target.name}")
        adjust_branch_stock(product, target.id, quantity, movement_type="transfer_in",
            source_type="branch_transfer", source_id=transfer.id, user_id=current_user.id,
            update_company_total=False, note=f"Transferencia desde {source.name}")
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
    branch = Branch.query.filter_by(id=branch_id, company_id=company_id, active=True).first_or_404()
    selected_ids = {int(value) for value in request.form.getlist("user_ids") if str(value).isdigit()}
    users = User.query.filter(User.company_id == company_id, User.role != "superadmin", User.id.in_(selected_ids)).all() if selected_ids else []
    if {int(user.id) for user in users} != selected_ids:
        abort(400)
    for row in UserBranchAccess.query.filter_by(branch_id=branch.id, company_id=company_id).all():
        db.session.delete(row)
    for user in users:
        db.session.add(UserBranchAccess(company_id=company_id, user_id=user.id, branch_id=branch.id))
    db.session.commit()
    flash(f"Accesos de {branch.name} actualizados.", "success")
    return redirect(url_for("company_billing.company_settings", panel="branches"))
