"""Blueprint de clientes: CRUD y API."""

import secrets
import re
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
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


def _record_client_whatsapp_consent(client, status, *, source, evidence=None):
    evidence_text = str(
        evidence if evidence is not None
        else request.form.get("whatsapp_marketing_consent_evidence") or ""
    ).strip()
    if status == "opted_in":
        raw_whatsapp = str(getattr(client, "whatsapp", None) or "").strip()
        from services.whatsapp_share_service import normalize_whatsapp_number
        normalized = normalize_whatsapp_number(raw_whatsapp)
        if not normalized or not 11 <= len(normalized) <= 15:
            raise ValueError("Para habilitar campañas, cargá un WhatsApp válido del cliente.")
        if not evidence_text:
            raise ValueError("Para habilitar campañas, registrá cómo y cuándo el cliente autorizó los mensajes de WhatsApp.")
        client.whatsapp = normalized
    from services.whatsapp_consent_service import record_whatsapp_consent
    if status == "opted_out":
        evidence_text = "Permiso de WhatsApp retirado desde el formulario de clientes."
    elif status == "unknown":
        evidence_text = "Estado de permiso de WhatsApp marcado como desconocido desde el formulario de clientes."
    record_whatsapp_consent(
        client,
        status,
        source=source,
        evidence=evidence_text,
        actor_user_id=getattr(current_user, "id", None),
    )


def _coerce_payload():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return request.form


def _price_lists_for_form(client=None):
    if getattr(current_user, "role", None) != "admin":
        return []
    from wholesale_pricing import PriceList, active_price_lists
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return []
    lists = active_price_lists(company_id)
    assigned_id = getattr(client, "price_list_id", None) if client is not None else None
    if assigned_id and all(int(row.id) != int(assigned_id) for row in lists):
        # Keep a pre-existing inactive/expired assignment visible while editing
        # the client, but never offer it as a new assignment.
        assigned = PriceList.query.filter_by(id=assigned_id, company_id=company_id).first()
        if assigned is not None:
            lists.append(assigned)
    return sorted(lists, key=lambda row: (str(row.name or "").lower(), int(row.id)))


def _apply_client_price_assignment(client):
    # Only the company administrator can change customer classification or tariff.
    if getattr(current_user, "role", None) != "admin":
        return
    from wholesale_pricing import PriceList, price_list_is_current

    customer_type = str(request.form.get("customer_type") or "minorista").strip().lower()
    if customer_type not in {"minorista", "mayorista"}:
        raise ValueError("El tipo de cliente no es válido.")
    raw_list_id = str(request.form.get("price_list_id") or "").strip()
    price_list = None
    if raw_list_id:
        try:
            list_id = int(raw_list_id)
        except (TypeError, ValueError):
            raise ValueError("La lista de precios seleccionada no es válida.")
        price_list = PriceList.query.filter_by(
            id=list_id,
            company_id=getattr(current_user, "company_id", None),
        ).first()
        current_list_can_remain = bool(
            price_list is not None
            and getattr(client, "price_list_id", None) == list_id
            and not price_list_is_current(price_list)
        )
        if price_list is None or (not price_list_is_current(price_list) and not current_list_can_remain):
            raise ValueError("La lista debe estar activa, dentro de su vigencia y pertenecer a tu empresa.")
    if customer_type == "mayorista" and price_list is None:
        raise ValueError("Para clasificar al cliente como mayorista, asignale una lista activa.")
    client.customer_type = customer_type
    client.price_list_id = price_list.id if customer_type == "mayorista" and price_list else None


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


@bp.post("/<int:client_id>/consent-link")
@tenant_required
def consent_link(client_id):
    """Generate or reuse a tenant-scoped public consent/preferences link."""
    from app import Client, db, record_audit, scope_query_to_company

    client = scope_query_to_company(Client.query, Client).filter(Client.id == client_id).first_or_404()
    if not any((client.email, client.whatsapp, client.phone)):
        flash("Cargá al menos un email, WhatsApp o teléfono antes de generar el enlace.", "warning")
        return redirect(url_for("clients.edit", id=client.id))

    if not client.marketing_consent_token:
        client.marketing_consent_token = secrets.token_urlsafe(48)
    record_audit(
        action="tenant_client_consent_link_generated",
        entity="client",
        entity_id=client.id,
        detail="Enlace público individual de preferencias comerciales generado o recuperado.",
        user_id=getattr(current_user, "id", None),
        company_id=getattr(current_user, "company_id", None),
    )
    db.session.commit()
    flash("Enlace de consentimiento generado. Podés copiarlo y compartirlo con el cliente.", "success")
    return redirect(url_for("clients.edit", id=client.id))


@bp.post("/<int:client_id>/marketing-consent")
@tenant_required
def marketing_consent(client_id):
    """Record a customer's already-expressed marketing choice from the tenant client profile."""
    from app import Client, db, record_audit, scope_query_to_company
    from stockarmobile.helpers.dates import utcnow_naive
    from services.whatsapp_consent_service import record_whatsapp_consent
    from services.whatsapp_share_service import normalize_whatsapp_number

    client = scope_query_to_company(Client.query, Client).filter(Client.id == client_id).first_or_404()
    action = str(request.form.get("action") or "").strip().lower()
    source = str(request.form.get("source") or "in_person").strip().lower()
    source_labels = {
        "in_person": "En el local / verbal",
        "public_consent_link": "Enlace de consentimiento",
        "qr_code": "Código QR",
        "written": "Formulario o documento",
        "other": "Otro medio",
    }
    if action not in {"accept_all", "email", "whatsapp", "phone", "reject_all"}:
        flash("La acción de consentimiento no es válida.", "danger")
        return redirect(url_for("clients.edit", id=client.id))
    if source not in source_labels:
        flash("Seleccioná un origen válido para el consentimiento.", "danger")
        return redirect(url_for("clients.edit", id=client.id))

    evidence = str(request.form.get("evidence") or "").strip()[:2000]
    if not evidence:
        flash("Contá brevemente cómo y cuándo el cliente expresó su decisión.", "warning")
        return redirect(url_for("clients.edit", id=client.id))

    normalized_whatsapp = normalize_whatsapp_number(str(client.whatsapp or "").strip())
    email_value = str(client.email or "").strip()
    phone_digits = "".join(character for character in str(client.phone or "") if character.isdigit())
    available = {
        "email": bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_value)),
        "whatsapp": bool(normalized_whatsapp and 11 <= len(normalized_whatsapp) <= 15),
        "phone": len(phone_digits) >= 7,
    }

    if action == "reject_all":
        selected_channels = ("email", "whatsapp", "phone")
        status = "opted_out"
    elif action == "accept_all":
        selected_channels = tuple(channel for channel, is_available in available.items() if is_available)
        status = "opted_in"
        if not selected_channels:
            flash("Cargá al menos un dato de contacto válido antes de autorizar comunicaciones.", "warning")
            return redirect(url_for("clients.edit", id=client.id))
    else:
        selected_channels = (action,)
        status = "opted_in"
        if not available[action]:
            labels = {"email": "un email válido", "whatsapp": "un WhatsApp válido", "phone": "un teléfono válido"}
            flash(f"Para autorizar este canal, cargá {labels[action]} en la ficha del cliente.", "warning")
            return redirect(url_for("clients.edit", id=client.id))

    now = utcnow_naive()
    evidence_text = f"Registrado desde la ficha del cliente ({source_labels[source]}): {evidence}"
    changed = []
    for channel in ("email", "phone"):
        if channel not in selected_channels:
            continue
        setattr(client, f"{channel}_marketing_consent", status)
        setattr(client, f"{channel}_marketing_consent_recorded_at", now)
        setattr(client, f"{channel}_marketing_consent_source", source)
        setattr(client, f"{channel}_marketing_consent_evidence", evidence_text)
        changed.append(channel)

    if "whatsapp" in selected_channels:
        if status == "opted_in":
            client.whatsapp = normalized_whatsapp
        record_whatsapp_consent(
            client,
            status,
            source=source,
            evidence=evidence_text,
            actor_user_id=getattr(current_user, "id", None),
        )
        changed.append("whatsapp")

    labels = {"email": "Email", "whatsapp": "WhatsApp", "phone": "Llamadas"}
    summary = ", ".join(labels[channel] for channel in changed)
    record_audit(
        action="tenant_client_marketing_consent_quick_update",
        entity="client",
        entity_id=client.id,
        detail=f"Permisos comerciales actualizados: {summary or 'ningún canal'} ({status}). Origen: {source}.",
        user_id=getattr(current_user, "id", None),
        company_id=getattr(current_user, "company_id", None),
    )
    db.session.commit()

    if action == "accept_all":
        skipped = [labels[channel] for channel, is_available in available.items() if not is_available]
        suffix = f" Sin dato válido: {', '.join(skipped)}." if skipped else ""
        flash(f"Autorización registrada para: {summary}.{suffix}", "success")
    elif action == "reject_all":
        flash("Se registró la baja de todos los canales comerciales.", "success")
    else:
        flash(f"Autorización registrada para {summary}.", "success")
    return redirect(url_for("clients.edit", id=client.id))


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
                    apply_record(client, record, actor_user_id=current_user.id)
                    if not was_active and client.active:
                        reactivated += 1
                    continue
                apply_record(client, record, actor_user_id=current_user.id)

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
    return render_template("clientes/form.html", client=None, price_lists=_price_lists_for_form())


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
    try:
        _apply_client_price_assignment(client)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("clients.index"))
    db.session.add(client)
    try:
        _record_client_whatsapp_consent(client, whatsapp_consent, source="client_form")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("clients.index"))
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
        current_whatsapp_consent = str(client.whatsapp_marketing_consent or "unknown")
        if whatsapp_consent != current_whatsapp_consent or request.form.get("whatsapp_marketing_consent_evidence"):
            try:
                _record_client_whatsapp_consent(client, whatsapp_consent, source="client_form")
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("clients.edit", id=client.id))
        client.birthday = _date_form("birthday")
        client.balance = _float_form("balance")
        client.credit_limit = _float_form("credit_limit")
        client.address = request.form.get("address") or None
        client.city = request.form.get("city") or None
        client.notes = request.form.get("notes") or None
        client.observations = request.form.get("observations") or None
        client.account_current_enabled = bool(request.form.get("account_current_enabled"))
        try:
            _apply_client_price_assignment(client)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("clients.edit", id=client.id))
        db.session.commit()
        flash("Cliente actualizado exitosamente.", "success")
        return redirect(url_for("clients.index"))
    return render_template("clientes/form.html", client=client, price_lists=_price_lists_for_form(client))


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
    whatsapp_consent = str(payload.get("whatsapp_marketing_consent") or "unknown").strip().lower()
    whatsapp_consent_evidence = str(payload.get("whatsapp_marketing_consent_evidence") or "").strip()
    if whatsapp_consent not in {"unknown", "opted_in", "opted_out"}:
        return jsonify({"error": "El estado de consentimiento de WhatsApp no es válido."}), 400
    if whatsapp_consent == "opted_in":
        from services.whatsapp_share_service import normalize_whatsapp_number
        normalized_whatsapp = normalize_whatsapp_number(whatsapp or "")
        if not normalized_whatsapp or not 11 <= len(normalized_whatsapp) <= 15:
            return jsonify({"error": "Para habilitar campañas, ingresá un WhatsApp válido."}), 400
        if not whatsapp_consent_evidence:
            return jsonify({"error": "Registrá cómo y cuándo el cliente autorizó recibir promociones."}), 400
        whatsapp = normalized_whatsapp
    customer_type = str(payload.get("customer_type") or "minorista").strip().lower()
    raw_price_list_id = payload.get("price_list_id")

    if customer_type not in {"minorista", "mayorista"}:
        return jsonify({"error": "Elegí un tipo de cliente válido."}), 400
    price_list = None
    if customer_type == "mayorista":
        if getattr(current_user, "role", None) != "admin":
            return jsonify({"error": "Solo un administrador puede asignar clientes a listas mayoristas."}), 403
        if raw_price_list_id in (None, ""):
            return jsonify({"error": "Para crear un cliente mayorista, seleccioná una lista de precios activa."}), 400
        try:
            parsed_price_list_id = int(raw_price_list_id)
        except (TypeError, ValueError):
            return jsonify({"error": "La lista mayorista seleccionada no es válida."}), 400
        from wholesale_pricing import PriceList, price_list_is_current
        price_list = PriceList.query.filter_by(
            id=parsed_price_list_id,
            company_id=getattr(current_user, "company_id", None),
        ).first()
        if price_list is None or not price_list_is_current(price_list):
            return jsonify({"error": "La lista debe estar activa, dentro de su vigencia y pertenecer a tu empresa."}), 400
    elif raw_price_list_id not in (None, "") and getattr(current_user, "role", None) == "admin":
        return jsonify({"error": "La lista mayorista solo se puede asignar a un cliente de tipo mayorista."}), 400

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
        customer_type=customer_type,
        price_list_id=price_list.id if customer_type == "mayorista" and price_list is not None else None,
        active=True,
    )
    db.session.add(client)
    try:
        db.session.flush()
        if whatsapp_consent != "unknown":
            _record_client_whatsapp_consent(
                client,
                whatsapp_consent,
                source="pos_quick_create",
                evidence=whatsapp_consent_evidence,
            )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("POS quick client creation failed for company_id=%s", getattr(current_user, "company_id", None))
        return jsonify({"error": "No se pudo crear el cliente. Intentá nuevamente."}), 500

    return jsonify({
        "client": {
            "id": client.id,
            "name": client.name,
            "email": client.email or "",
            "whatsapp": client.whatsapp or "",
            "customer_type": client.customer_type,
            "price_list_id": client.price_list_id,
            "price_list_name": price_list.name if price_list is not None else "",
        }
    }), 201
