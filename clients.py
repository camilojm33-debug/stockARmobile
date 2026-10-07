"""Blueprint de clientes: CRUD y API."""

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for
from io import BytesIO
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


def _marketing_consent_form_value(name, current="unknown"):
    value = request.form.get(name)
    value = str(current if value is None else value).strip().lower()
    if value not in {"unknown", "opted_in", "opted_out"}:
        raise ValueError("El estado de consentimiento de marketing no es válido.")
    return value


def _coerce_payload():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return request.form


def _client_stats_for_export(clients, Sale, Quote, db, scope_query_to_company):
    client_ids = {client.id for client in clients}
    stats = {client_id: {} for client_id in client_ids}
    if not client_ids:
        return stats

    sales_rows = (
        scope_query_to_company(
            db.session.query(
                Sale.client_id,
                db.func.count(Sale.id).label("purchase_count"),
                db.func.coalesce(db.func.sum(Sale.total_amount), 0).label("total_spent"),
            ),
            Sale,
        )
        .filter(Sale.client_id.in_(client_ids))
        .group_by(Sale.client_id)
        .all()
    )
    for row in sales_rows:
        stats[row.client_id] = {
            **stats.get(row.client_id, {}),
            "purchase_count": int(row.purchase_count or 0),
            "total_spent": float(row.total_spent or 0),
        }

    quote_rows = (
        scope_query_to_company(
            db.session.query(
                Quote.client_id,
                db.func.count(Quote.id).label("quote_count"),
                db.func.coalesce(db.func.sum(Quote.total_amount), 0).label("total_quoted"),
            ),
            Quote,
        )
        .filter(Quote.client_id.in_(client_ids))
        .group_by(Quote.client_id)
        .all()
    )
    for row in quote_rows:
        stats[row.client_id] = {
            **stats.get(row.client_id, {}),
            "quote_count": int(row.quote_count or 0),
            "total_quoted": float(row.total_quoted or 0),
        }
    return stats


@bp.route("/export.xlsx")
@tenant_required
def export_excel():
    from app import Client, Quote, Sale, db, scope_query_to_company
    from services.client_excel_service import create_workbook

    clients = scope_query_to_company(Client.query, Client).order_by(Client.name).all()
    client_stats = _client_stats_for_export(clients, Sale, Quote, db, scope_query_to_company)
    buffer = create_workbook(clients=clients, client_stats=client_stats)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"clientes_{__import__('datetime').datetime.utcnow():%Y%m%d}.xlsx",
    )


@bp.route("/import/template.xlsx")
@tenant_required
def import_template():
    from app import Client
    from services.client_excel_service import create_workbook

    empty_stats = {}
    buffer = create_workbook(clients=[], client_stats=empty_stats)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="plantilla_clientes_stockarmobile.xlsx",
    )


@bp.route("/import", methods=["POST"])
@tenant_required
def import_excel():
    from app import Client, db, scope_query_to_company
    from services.client_excel_service import (
        ClientImportError,
        _build_identity_lookup,
        apply_record,
        build_records,
        resolve_existing,
    )
    from services.plan_usage_service import PlanUsageService

    upload = request.files.get("file")
    try:
        records = build_records(upload)
        lookups = _build_identity_lookup(Client, scope_query_to_company, records)
        existing = [resolve_existing(record, lookups) for record in records]

        active_creates = sum(1 for record, client in zip(records, existing) if client is None and record["active"])
        active_reactivations = sum(
            1 for record, client in zip(records, existing)
            if client is not None and not client.active and record["active"]
        )
        if active_creates or active_reactivations:
            usage = PlanUsageService.usage_snapshot(getattr(current_user, "company_id", None))
            metric = next(
                (item for item in usage["metrics"] if item.key == PlanUsageService.RESOURCE_CLIENTS),
                None,
            )
            if metric and metric.limit > 0 and metric.used + active_creates + active_reactivations > metric.limit:
                raise ClientImportError(
                    f"La importación supera el límite de clientes de tu plan. "
                    f"Disponibles: {metric.remaining}; necesita: {active_creates + active_reactivations}."
                )

        created = 0
        updated = 0
        reactivated = 0

        try:
            for record, client in zip(records, existing):
                if client is None:
                    client = Client(
                        company_id=getattr(current_user, "company_id", None),
                        name=record["name"],
                        active=record["active"],
                    )
                    db.session.add(client)
                    created += 1
                else:
                    was_active = bool(client.active)
                    updated += 1
                    apply_record(client, record)
                    if not was_active and client.active:
                        reactivated += 1
                    continue
                apply_record(client, record)

            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        summary = f"Importación completada: {created} creados y {updated} actualizados."
        if reactivated:
            summary += f" {reactivated} reactivados."
        flash(summary, "success")
    except ClientImportError as exc:
        flash(f"No se importó el archivo: {exc}", "danger")
    except Exception:
        db.session.rollback()
        flash("No se importó el archivo. No se aplicaron cambios.", "danger")

    return redirect(url_for("clients.index"))


@bp.route("/")
@tenant_required
def index():
    from app import Client, Quote, Sale, db, scope_query_to_company

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
    quote_rows = (
        scope_query_to_company(
            db.session.query(
                Quote.client_id,
                db.func.count(Quote.id).label("quote_count"),
                db.func.coalesce(db.func.sum(Quote.total_amount), 0).label("total_quoted"),
                db.func.max(Quote.date).label("last_quote_at"),
            ),
            Quote,
        )
        .filter(Quote.client_id.isnot(None))
        .group_by(Quote.client_id)
        .all()
    )
    for row in quote_rows:
        client_stats.setdefault(row.client_id, {})
        client_stats[row.client_id].update({"quote_count": row.quote_count, "total_quoted": float(row.total_quoted or 0), "last_quote_at": row.last_quote_at})
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

    try:
        email_consent = _marketing_consent_form_value("email_marketing_consent")
        whatsapp_consent = _marketing_consent_form_value("whatsapp_marketing_consent")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("clients.index"))

    client = Client(
        company_id=getattr(current_user, "company_id", None),
        name=request.form.get("name", "").strip(),
        email=request.form.get("email") or None,
        phone=request.form.get("phone") or None,
        whatsapp=request.form.get("whatsapp") or None,
        email_marketing_consent=email_consent,
        whatsapp_marketing_consent=whatsapp_consent,
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
        try:
            email_consent = _marketing_consent_form_value(
                "email_marketing_consent", client.email_marketing_consent
            )
            whatsapp_consent = _marketing_consent_form_value(
                "whatsapp_marketing_consent", client.whatsapp_marketing_consent
            )
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("clients.edit", id=client.id))

        client.name = request.form.get("name", client.name).strip()
        client.email = request.form.get("email") or None
        client.phone = request.form.get("phone") or None
        client.whatsapp = request.form.get("whatsapp") or None
        client.email_marketing_consent = email_consent
        client.whatsapp_marketing_consent = whatsapp_consent
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
    from app import Client, Quote, Sale, db, scope_query_to_company

    client = scope_query_to_company(Client.query, Client).filter(Client.id == id).first_or_404()
    sales_stats = scope_query_to_company(
        db.session.query(db.func.count(Sale.id).label("sales_count"), db.func.coalesce(db.func.sum(Sale.total_amount), 0).label("total_spent")),
        Sale,
    ).filter(Sale.client_id == client.id).first()
    quote_stats = scope_query_to_company(
        db.session.query(db.func.count(Quote.id).label("quote_count"), db.func.coalesce(db.func.sum(Quote.total_amount), 0).label("total_quoted"), db.func.max(Quote.date).label("last_quote_at")),
        Quote,
    ).filter(Quote.client_id == client.id).first()
    last_quote = scope_query_to_company(Quote.query, Quote).filter(Quote.client_id == client.id).order_by(Quote.date.desc()).first()
    return render_template(
        "clientes/form.html",
        client=client,
        readonly=True,
        sales_stats=sales_stats,
        quote_stats=quote_stats,
        last_quote=last_quote,
    )


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


@bp.route("/api/quick-create", methods=["POST"])
@tenant_required
def api_quick_create():
    from app import Client, db, scope_query_to_company
    from services.plan_usage_service import PlanUsageService

    payload = _coerce_payload()
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip() or None
    whatsapp = (payload.get("whatsapp") or "").strip() or None

    if not name:
        return jsonify({"error": "El nombre del cliente es obligatorio."}), 400

    allowed, message = PlanUsageService.can_create(getattr(current_user, "company_id", None), PlanUsageService.RESOURCE_CLIENTS)
    if not allowed:
        return jsonify({"error": message}), 403

    if email:
        exists = scope_query_to_company(Client.query.filter_by(email=email, active=True), Client).first()
        if exists is not None:
            return jsonify({"error": "Ya existe un cliente activo con ese email."}), 409

    client = Client(
        company_id=getattr(current_user, "company_id", None),
        name=name,
        email=email,
        whatsapp=whatsapp,
        active=True,
    )
    db.session.add(client)
    db.session.commit()

    return jsonify({
        "client": {
            "id": client.id,
            "name": client.name,
            "email": client.email or "",
            "whatsapp": client.whatsapp or "",
        }
    }), 201
