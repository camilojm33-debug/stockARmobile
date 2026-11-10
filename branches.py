"""Premium multi-branch management routes."""
import re
import secrets
import time
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, session, url_for
from flask_login import current_user
from sqlalchemy import func, or_
from app import Branch, Company, User, UserBranchAccess, db, tenant_required, company_admin_required
from stockarmobile.branch_context import SESSION_KEY

bp = Blueprint("branches", __name__, url_prefix="/sucursales")

def _safe_return_path(value, fallback="/"):
    """Accept only an application-local path for post/selection redirects."""
    candidate = str(value or "").strip()
    if (not candidate.startswith("/") or candidate.startswith("//")
            or "\\" in candidate or any(ord(char) < 32 for char in candidate)):
        return fallback
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback
    return candidate


def _company_id():
    return int(getattr(current_user, "company_id", 0) or 0)


PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY = "stockarmobile_pending_branch_user_assignment"
PENDING_BRANCH_USER_ASSIGNMENT_TTL_SECONDS = 900
MAX_PENDING_BRANCH_USER_ASSIGNMENTS = 200


def _parse_selected_user_ids(raw_values):
    selected_ids = set()
    for raw_value in raw_values or []:
        try:
            user_id = int(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError("La selección de empleados contiene un identificador inválido.") from None
        if user_id <= 0:
            raise ValueError("La selección de empleados contiene un identificador inválido.")
        selected_ids.add(user_id)
    if len(selected_ids) > MAX_PENDING_BRANCH_USER_ASSIGNMENTS:
        raise ValueError(
            f"Podés asignar hasta {MAX_PENDING_BRANCH_USER_ASSIGNMENTS} empleados a una sucursal por vez."
        )
    return selected_ids


def _apply_branch_user_access(company_id, branch_id, selected_ids):
    """Persist an entire branch membership selection after validating tenant ownership."""
    branch = Branch.query.filter_by(
        id=int(branch_id), company_id=int(company_id), active=True
    ).first()
    if branch is None:
        raise ValueError("La sucursal ya no existe o está inactiva. No se modificaron los accesos.")

    selected_ids = {int(value) for value in (selected_ids or [])}
    from stockarmobile.company_memberships import get_company_users
    eligible_users = get_company_users(int(company_id), active_only=True)
    eligible_by_id = {int(user.id): user for user in eligible_users if user.role != "superadmin"}
    if not selected_ids.issubset(set(eligible_by_id)):
        raise ValueError(
            "Uno o más empleados ya no están activos o no pertenecen a esta empresa. No se modificaron los accesos."
        )
    users = [eligible_by_id[user_id] for user_id in sorted(selected_ids)]

    existing_rows = UserBranchAccess.query.filter_by(
        branch_id=branch.id, company_id=int(company_id)
    ).all()
    for row in existing_rows:
        db.session.delete(row)
    db.session.flush()

    for user in users:
        db.session.add(UserBranchAccess(
            company_id=int(company_id),
            user_id=int(user.id),
            branch_id=int(branch.id),
        ))
    db.session.flush()
    return branch, users


def _lock_company_for_branch_limit(company_id):
    """Serialize branch creation/reactivation for a company on databases with row locks."""
    return (
        Company.query.filter_by(id=company_id, active=True)
        .with_for_update()
        .populate_existing()
        .first_or_404()
    )


def _has_pending_ai_vendor_orders(company_id, branch_id):
    """Protect unresolved AI orders and their online payment attempts before branch closure."""
    from app import Payment, Quote

    ai_quotes = Quote.query.filter_by(
        company_id=int(company_id),
        branch_id=int(branch_id),
        observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
    )
    terminal_statuses = (
        "ANULADO", "ANULADA", "CANCELADO", "CANCELADA", "CANCELED",
        "CANCELLED", "RECHAZADO", "RECHAZADA", "REJECTED", "VENCIDO",
        "VENCIDA", "EXPIRED", "CONVERTIDO", "CLOSED",
    )
    if (
        ai_quotes.filter(Quote.converted_sale_id.is_(None))
        .filter(func.upper(func.coalesce(Quote.status, "")).notin_(terminal_statuses))
        .first()
        is not None
    ):
        return True

    quote_ids = [int(value) for (value,) in ai_quotes.with_entities(Quote.id).all()]
    if not quote_ids:
        return False
    references = [
        Payment.external_reference.like(
            f"flow:ai_order|company_id:{int(company_id)}|quote_id:{quote_id}|%"
        )
        for quote_id in quote_ids
    ]
    return (
        Payment.query.filter(
            Payment.company_id == int(company_id),
            Payment.provider == "mercadopago_ai_order",
            Payment.status.in_(("pending", "in_process", "authorized")),
            or_(*references),
        ).first()
        is not None
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
    return redirect(_safe_return_path(request.form.get("return_to"), fallback=url_for("dashboard.index")))

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
        if _has_pending_ai_vendor_orders(company_id, branch.id):
            flash(
                "Completá o cancelá los pedidos pendientes del Vendedor IA y resolvé sus pagos antes de desactivar la sucursal.",
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

@bp.route("/api/productos-transferencia")
@company_admin_required
def search_transfer_products():
    """Search this tenant's products and return stock for the selected source branch."""
    from app import BranchStock, Product
    from services.ai_agent.usage_service import can_use_commercial_feature

    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    feature = can_use_commercial_feature(company, "multi_branch")
    if not feature.allowed:
        return jsonify(success=False, error=feature.reason or "La gestión multisucursal requiere un plan habilitado."), 403

    source_id = request.args.get("source_branch_id", type=int)
    source = Branch.query.filter_by(id=source_id, company_id=company_id, active=True).first()
    if source is None:
        return jsonify(success=False, error="Seleccioná una sucursal de origen válida."), 400

    search = (request.args.get("q") or "").strip()[:100]
    category = (request.args.get("category") or "").strip()[:100]
    available_only = request.args.get("available_only", "1") != "0"
    limit = min(100, max(1, request.args.get("limit", 30, type=int) or 30))
    offset = min(50000, max(0, request.args.get("offset", 0, type=int) or 0))

    product_query = Product.query.filter_by(company_id=company_id, active=True)
    if search:
        pattern = f"%{search}%"
        product_query = product_query.filter(or_(
            Product.name.ilike(pattern),
            Product.barcode.ilike(pattern),
            Product.category.ilike(pattern),
            Product.brand.ilike(pattern),
        ))
    if category:
        product_query = product_query.filter(func.lower(func.trim(Product.category)) == category.lower())
    if available_only:
        available_product_ids = db.session.query(BranchStock.product_id).filter_by(
            company_id=company_id, branch_id=source.id
        ).filter(BranchStock.stock > 0)
        product_query = product_query.filter(Product.id.in_(available_product_ids))

    categories = sorted({
        row[0] for row in db.session.query(Product.category)
        .filter_by(company_id=company_id, active=True)
        .filter(Product.category.isnot(None), func.trim(Product.category) != "")
        .distinct()
        .all()
    }, key=lambda value: value.casefold())
    total = product_query.count()
    products = product_query.order_by(func.lower(Product.name), Product.id).offset(offset).limit(limit).all()
    product_ids = [product.id for product in products]
    stock_rows = {}
    if product_ids:
        stock_rows = {
            row.product_id: row
            for row in BranchStock.query.filter_by(company_id=company_id, branch_id=source.id)
            .filter(BranchStock.product_id.in_(product_ids)).all()
        }

    return jsonify(
        success=True,
        products=[{
            "id": product.id,
            "name": product.name,
            "barcode": product.barcode or "",
            "category": product.category or "",
            "brand": product.brand or "",
            "unit_measure": product.unit_measure or "u",
            "stock": float(stock_rows[product.id].stock or 0) if product.id in stock_rows else 0,
        } for product in products],
        categories=categories,
        total=total,
        offset=offset,
        limit=limit,
        has_more=offset + len(products) < total,
        source_branch_id=source.id,
    )


@bp.route("/transferir-stock", methods=["POST"])
@company_admin_required
def transfer_stock():
    import json
    from decimal import Decimal, InvalidOperation
    from app import Product

    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    pin_blocked = _require_company_pin(company)
    if pin_blocked:
        return pin_blocked

    return_path = _safe_return_path(
        request.form.get("return_to"),
        fallback=url_for("company_billing.company_settings", panel="branches"),
    )
    source_id = request.form.get("source_branch_id", type=int)
    target_id = request.form.get("target_branch_id", type=int)
    source = Branch.query.filter_by(id=source_id, company_id=company_id, active=True).first()
    target = Branch.query.filter_by(id=target_id, company_id=company_id, active=True).first()
    if source is None or target is None or source.id == target.id:
        flash("Revisá las sucursales de origen y destino. Deben ser distintas y estar activas.", "danger")
        return redirect(return_path)

    transfer_succeeded = False
    try:
        raw_items = (request.form.get("items_json") or "").strip()
        if raw_items:
            payload = json.loads(raw_items)
            if not isinstance(payload, list) or not payload:
                raise ValueError("Agregá al menos un producto a la transferencia.")
            if len(payload) > 100:
                raise ValueError("Una transferencia puede incluir hasta 100 productos por operación.")
            parsed_items = []
            seen_ids = set()
            for row in payload:
                if not isinstance(row, dict):
                    raise ValueError("La lista de productos no tiene un formato válido.")
                try:
                    product_id = int(row.get("product_id") or 0)
                except (TypeError, ValueError):
                    product_id = 0
                if product_id <= 0:
                    raise ValueError("Hay un producto inválido en la lista.")
                if product_id in seen_ids:
                    raise ValueError("No repitas un producto en la misma transferencia.")
                seen_ids.add(product_id)
                try:
                    quantity = Decimal(str(row.get("quantity") or "0").replace(",", "."))
                except (InvalidOperation, AttributeError):
                    raise ValueError("Hay una cantidad inválida en la lista.") from None
                parsed_items.append((product_id, quantity))

            products_by_id = {
                product.id: product for product in Product.query.filter_by(
                    company_id=company_id, active=True
                ).filter(Product.id.in_(seen_ids)).all()
            }
            if len(products_by_id) != len(parsed_items):
                raise ValueError("Uno o más productos ya no están disponibles para esta empresa.")
            transfer_items = [(products_by_id[product_id], quantity) for product_id, quantity in parsed_items]
        else:
            # Keep compatibility with the original single-product form.
            from_reference = (request.form.get("product_code") or request.form.get("product_id") or "").strip()
            product = Product.query.filter_by(company_id=company_id, active=True).filter_by(barcode=from_reference).first() if from_reference else None
            if product is None and from_reference.isdigit():
                product = Product.query.filter_by(id=int(from_reference), company_id=company_id, active=True).first()
            if product is None:
                raise ValueError("Revisá el producto de la transferencia.")
            try:
                quantity = Decimal((request.form.get("quantity") or "0").replace(",", "."))
            except (InvalidOperation, AttributeError):
                raise ValueError("La cantidad de transferencia no es válida.") from None
            transfer_items = [(product, quantity)]

        from stockarmobile.branch_inventory import transfer_branch_stock_batch
        idempotency_key = (str(request.form.get("idempotency_key") or "").strip() or secrets.token_urlsafe(24))[:120]
        transfers = transfer_branch_stock_batch(
            transfer_items, source.id, target.id,
            user_id=current_user.id, note=request.form.get("note"),
            idempotency_key=idempotency_key,
        )
        db.session.commit()
        transfer_succeeded = True
        total_units = sum((quantity for _product, quantity in transfer_items), Decimal("0"))
        flash(
            f"Transferencia completada: {len(transfers)} producto(s), "
            f"{total_units.normalize()} unidad(es) de {source.name} a {target.name}.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Branch stock transfer batch failed company_id=%s", company_id)
        flash("No se pudo completar la transferencia. No se guardaron cambios.", "danger")
    if transfer_succeeded:
        separator = "&" if "?" in return_path else "?"
        return_path = f"{return_path}{separator}transfer_status=success"
    return redirect(return_path)


@bp.route("/<int:branch_id>/usuarios", methods=["POST"])
@company_admin_required
def assign_users(branch_id):
    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked

    branch = Branch.query.filter_by(
        id=branch_id, company_id=company_id, active=True
    ).first_or_404()
    try:
        selected_ids = _parse_selected_user_ids(request.form.getlist("user_ids"))
        from stockarmobile.company_memberships import get_company_users
        eligible_users = get_company_users(company_id, active_only=True)
        eligible_by_id = {int(user.id): user for user in eligible_users if user.role != "superadmin"}
        if not selected_ids.issubset(set(eligible_by_id)):
            raise ValueError(
                "Uno o más empleados ya no están activos o no pertenecen a esta empresa."
            )
        selectable_users = [eligible_by_id[user_id] for user_id in sorted(selected_ids)]
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))

    from company_billing import _is_pin_verified
    if not _is_pin_verified(company_id):
        session[PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY] = {
            "company_id": company_id,
            "owner_user_id": int(current_user.id),
            "branch_id": int(branch.id),
            "user_ids": sorted(selected_ids),
            "created_at": time.time(),
        }
        session.modified = True
        if not company.business_pin_hash:
            flash(
                "La selección de personal quedó guardada. Generá primero el PIN administrativo para confirmar la asignación.",
                "warning",
            )
        else:
            flash(
                "La selección de personal quedó pendiente. Validá el PIN y se aplicará automáticamente a esta sucursal.",
                "warning",
            )
        return redirect(url_for("company_billing.company_settings", panel="branches"))

    try:
        branch, users = _apply_branch_user_access(company_id, branch.id, selected_ids)
        from app import record_audit
        record_audit(
            action="branch_user_access_update",
            entity="branch",
            entity_id=branch.id,
            company_id=company_id,
            detail=f"Asignación de personal actualizada: {len(users)} empleado(s) habilitado(s).",
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Branch employee assignment failed company_id=%s branch_id=%s",
            company_id,
            branch_id,
        )
        flash("No se pudieron guardar los accesos. No se modificó la asignación anterior.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))

    flash(
        f"Accesos de {branch.name} actualizados: {len(users)} empleado(s) habilitado(s).",
        "success",
    )
    return redirect(url_for("company_billing.company_settings", panel="branches"))



@bp.route("/<int:branch_id>/ticket-settings", methods=["POST"])
@company_admin_required
def update_branch_ticket_settings(branch_id):
    """Save branch-owned ticket identity and printer overrides."""
    import json
    from app import record_audit

    company_id = _company_id()
    company = Company.query.filter_by(id=company_id, active=True).first_or_404()
    blocked = _require_feature(company)
    if blocked:
        return blocked
    pin_blocked = _require_company_pin(company)
    if pin_blocked:
        return pin_blocked

    branch = Branch.query.filter_by(id=branch_id, company_id=company_id).first_or_404()
    try:
        current = json.loads(branch.receipt_settings_json or "{}")
    except (TypeError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}

    text_fields = {
        "ticket_name": 120,
        "logo": 255,
        "footer": 400,
        "printer_name": 160,
        "printer_type": 20,
        "printer_host": 120,
        "paper_size": 20,
    }
    for key, max_length in text_fields.items():
        current[key] = (request.form.get(key) or "").strip()[:max_length]

    printer_type = current.get("printer_type", "").lower()
    if printer_type not in {"", "browser", "thermal"}:
        flash("Seleccioná un modo de impresión válido.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))
    current["printer_type"] = printer_type

    if current.get("paper_size", "") not in {"", "A4", "Ticket80", "Carta"}:
        flash("Seleccioná un tamaño de papel válido.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="branches"))

    raw_port = (request.form.get("printer_port") or "").strip()
    if raw_port:
        try:
            printer_port = int(raw_port)
        except (TypeError, ValueError):
            printer_port = 0
        if not 1 <= printer_port <= 65535:
            flash("El puerto de impresión debe estar entre 1 y 65535.", "warning")
            return redirect(url_for("company_billing.company_settings", panel="branches"))
        current["printer_port"] = printer_port
    else:
        current["printer_port"] = ""

    drawer_choice = request.form.get("cashdrawer_enabled", "")
    if drawer_choice == "1":
        current["cashdrawer_enabled"] = True
    elif drawer_choice == "0":
        current["cashdrawer_enabled"] = False
    else:
        current.pop("cashdrawer_enabled", None)

    try:
        branch.receipt_settings_json = json.dumps(current, ensure_ascii=False)
        record_audit(
            action="branch_ticket_settings_update",
            entity="branch",
            entity_id=branch.id,
            company_id=company_id,
            detail=f"Configuración de ticket e impresión actualizada para {branch.name}.",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Branch ticket settings update failed company_id=%s branch_id=%s",
            company_id,
            branch_id,
        )
        flash("No se pudo guardar la configuración de ticket. No se modificaron los datos anteriores.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="branches"))

    flash(f"Configuración de ticket e impresión guardada para {branch.name}.", "success")
    return redirect(url_for("company_billing.company_settings", panel="branches"))
