"""Blueprint de clientes: CRUD y API."""

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app import tenant_required

bp = Blueprint("clients", __name__)


def _float_form(name, default=0.0):
    try:
        return float(request.form.get(name) or default)
    except (TypeError, ValueError):
        return default


def _date_form(name):
    from datetime import datetime

    value = request.form.get(name)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


@bp.route("/")
@tenant_required
def index():
    from app import Client, Sale, db, scope_query_to_company

    query = scope_query_to_company(Client.query.filter_by(active=True), Client)
    search = request.args.get("search")
    if search:
        like = f"%{search}%"
        query = query.filter((Client.name.ilike(like)) | (Client.email.ilike(like)) | (Client.phone.ilike(like)) | (Client.whatsapp.ilike(like)))
    clients = query.order_by(Client.name).all()
    stats_rows = (
        scope_query_to_company(
            db.session.query(
                Sale.client_id,
                db.func.count(Sale.id).label("purchase_count"),
                db.func.coalesce(db.func.sum(Sale.total_amount), 0).label("total_spent"),
            ),
            Sale,
        )
        .filter(Sale.client_id.isnot(None))
        .group_by(Sale.client_id)
        .all()
    )
    client_stats = {row.client_id: {"purchase_count": row.purchase_count, "total_spent": float(row.total_spent or 0)} for row in stats_rows}
    return render_template("clientes/index.html", clients=clients, client_stats=client_stats)


@bp.route("/new", methods=["GET"])
@bp.route("/add", methods=["GET"])
@tenant_required
def new():
    return render_template("clientes/form.html", client=None)


@bp.route("/add", methods=["POST"])
@bp.route("/post", methods=["POST"])
@tenant_required
def post():
    from app import Client, db, scope_query_to_company
    from services.plan_usage_service import PlanUsageService

    allowed, message = PlanUsageService.can_create(getattr(current_user, "company_id", None), PlanUsageService.RESOURCE_CLIENTS)
    if not allowed:
        flash(message, "warning")
        return redirect(url_for("company_billing.subscription_portal"))

    client = Client(
        company_id=getattr(current_user, "company_id", None),
        name=request.form.get("name", "").strip(),
        email=request.form.get("email") or None,
        phone=request.form.get("phone") or None,
        whatsapp=request.form.get("whatsapp") or None,
        birthday=_date_form("birthday"),
        balance=_float_form("balance"),
        credit_limit=_float_form("credit_limit"),
        address=request.form.get("address") or None,
        city=request.form.get("city") or None,
        notes=request.form.get("notes") or None,
        observations=request.form.get("observations") or None,
        account_current_enabled=bool(request.form.get("account_current_enabled")),
    )
    if not client.name:
        flash("El nombre del cliente es obligatorio.", "danger")
        return redirect(url_for("clients.index"))
    db.session.add(client)
    db.session.commit()
    flash("Cliente creado exitosamente.", "success")
    return redirect(url_for("clients.index"))


@bp.route("/edit/<int:client_id>", methods=["GET", "POST"])
@bp.route("/edit/<int:id>", methods=["GET", "POST"])
@tenant_required
def edit(client_id=None, id=None):
    from app import Client, db, scope_query_to_company

    client = scope_query_to_company(db.session.query(Client), Client).filter(Client.id == (client_id or id)).first()
    if client is None:
        flash("Cliente no encontrado.", "warning")
        return redirect(url_for("clients.index"))
    if request.method == "POST":
        client.name = request.form.get("name", client.name).strip()
        client.email = request.form.get("email") or None
        client.phone = request.form.get("phone") or None
        client.whatsapp = request.form.get("whatsapp") or None
        client.birthday = _date_form("birthday")
        client.balance = _float_form("balance")
        client.credit_limit = _float_form("credit_limit")
        client.address = request.form.get("address") or None
        client.city = request.form.get("city") or None
        client.notes = request.form.get("notes") or None
        client.observations = request.form.get("observations") or None
        client.account_current_enabled = bool(request.form.get("account_current_enabled"))
        db.session.commit()
        flash("Cliente actualizado exitosamente.", "success")
        return redirect(url_for("clients.index"))
    return render_template("clientes/form.html", client=client)


@bp.route("/show/<int:id>")
@tenant_required
def show(id):
    from app import Client, scope_query_to_company

    client = scope_query_to_company(Client.query, Client).filter(Client.id == id).first_or_404()
    return render_template("clientes/form.html", client=client, readonly=True)


@bp.route("/delete/<int:client_id>", methods=["POST"])
@bp.route("/delete/<int:id>", methods=["POST"])
@tenant_required
def delete(client_id=None, id=None):
    from app import Client, db, scope_query_to_company

    client = scope_query_to_company(db.session.query(Client), Client).filter(Client.id == (client_id or id)).first()
    if client:
        client.active = False
        db.session.commit()
        flash("Cliente desactivado exitosamente.", "success")
    return redirect(url_for("clients.index"))


@bp.route("/api/clients")
@tenant_required
def api_list():
    from app import Client, scope_query_to_company

    return jsonify(
        {
            "clients": [
                {
                    "id": c.id,
                    "name": c.name,
                    "email": c.email or "",
                    "phone": c.phone or "",
                    "whatsapp": c.whatsapp or "",
                    "balance": float(c.balance or 0),
                    "credit_limit": float(c.credit_limit or 0),
                    "address": c.address or "",
                    "city": c.city or "",
                }
                for c in scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all()
            ]
        }
    )


@bp.route("/api/<int:client_id>")
@tenant_required
def api_get(client_id):
    from app import Client, scope_query_to_company

    c = scope_query_to_company(Client.query, Client).filter(Client.id == client_id).first_or_404()
    return jsonify(
        {
            "id": c.id,
            "name": c.name,
            "email": c.email or "",
            "phone": c.phone or "",
            "whatsapp": c.whatsapp or "",
            "balance": float(c.balance or 0),
            "credit_limit": float(c.credit_limit or 0),
            "address": c.address or "",
        }
    )
