"""CRM comercial para empresas tenant de StockArMobile.

IMPORTANTE: no contiene ni reutiliza entidades del CRM de SuperAdmin.
"""
from datetime import datetime, timedelta
from urllib.parse import urlencode
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, or_

from app import db, tenant_required, scope_query_to_company
from stockarmobile.decorators import ai_pro_crm_required
from crm_models import CRMActivity, CRMOpportunity

bp = Blueprint("crm", __name__)

STAGES = (
    ("nuevo", "Nuevo"),
    ("contactado", "Contactado"),
    ("interesado", "Interesado"),
    ("presupuesto", "Presupuesto"),
    ("negociacion", "Negociación"),
    ("ganado", "Ganado"),
    ("perdido", "Perdido"),
)
STAGE_LABELS = dict(STAGES)
ACTIVITY_TYPES = (
    ("llamada", "Llamada"),
    ("whatsapp", "WhatsApp"),
    ("email", "Email"),
    ("reunion", "Reunión"),
    ("tarea", "Tarea"),
    ("nota", "Nota"),
    ("seguimiento", "Seguimiento"),
)


def _company_id():
    return getattr(current_user, "company_id", None)


def _now():
    return datetime.utcnow()


def _opportunity_query():
    return scope_query_to_company(CRMOpportunity.query, CRMOpportunity)


def _activity_query():
    return scope_query_to_company(CRMActivity.query, CRMActivity)


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _client_for_company(client_id):
    from app import Client
    return scope_query_to_company(Client.query, Client).filter(Client.id == client_id).first()


def _stage_status(stage):
    return "closed" if stage in {"ganado", "perdido"} else "open"


def _archive_loss_reason(opportunity, *, notes_override=None):
    previous = (getattr(opportunity, "loss_reason", None) or "").strip()
    notes = (notes_override if notes_override is not None else opportunity.notes) or ""
    notes = str(notes).strip()
    if previous:
        marker = f"Motivo de pérdida anterior: {previous}"
        if marker not in notes:
            notes = f"{notes}\n{marker}".strip()
    opportunity.notes = notes or None
    opportunity.loss_reason = None


def _resolve_commercial_links(client_id, quote_id, sale_id, opportunity_id=None):
    """Resolve quote/sale links strictly inside this tenant and for the same client."""
    from app import Quote, Sale

    quote = None
    sale = None
    if quote_id:
        quote = scope_query_to_company(Quote.query, Quote).filter(Quote.id == quote_id).first()
        if quote is None or int(quote.client_id or 0) != int(client_id):
            raise ValueError("El presupuesto debe pertenecer al mismo cliente y empresa.")
        duplicate = _opportunity_query().filter(CRMOpportunity.quote_id == quote.id)
        if opportunity_id:
            duplicate = duplicate.filter(CRMOpportunity.id != opportunity_id)
        if duplicate.first():
            raise ValueError("Ese presupuesto ya está vinculado a otra oportunidad del CRM.")

    if sale_id:
        sale = scope_query_to_company(Sale.query, Sale).filter(Sale.id == sale_id).first()
        if sale is None or int(sale.client_id or 0) != int(client_id):
            raise ValueError("La venta debe pertenecer al mismo cliente y empresa.")
        sale_is_annulled = str(sale.status or "").strip().lower() in {"anulada", "cancelada", "cancelled", "voided"}
        if sale_is_annulled:
            already_linked_here = opportunity_id and _opportunity_query().filter(
                CRMOpportunity.id == opportunity_id,
                CRMOpportunity.sale_id == sale.id,
            ).first()
            if not already_linked_here:
                raise ValueError("No se puede vincular una venta anulada a una oportunidad.")
        if quote is not None and quote.converted_sale_id and int(quote.converted_sale_id) != int(sale.id):
            raise ValueError("La venta seleccionada no corresponde al presupuesto vinculado.")
    elif quote is not None and quote.converted_sale_id:
        sale = scope_query_to_company(Sale.query, Sale).filter(Sale.id == quote.converted_sale_id).first()
        if sale is not None and int(sale.client_id or 0) != int(client_id):
            raise ValueError("La venta convertida no corresponde al cliente de la oportunidad.")

    return quote, sale


def _opportunity_form_options():
    from app import Client, Quote, Sale, User

    return {
        "clients": scope_query_to_company(Client.query.filter(Client.active.is_(True)), Client).order_by(Client.name).all(),
        "quotes": scope_query_to_company(Quote.query, Quote).order_by(Quote.date.desc(), Quote.id.desc()).limit(250).all(),
        "sales": scope_query_to_company(Sale.query, Sale).order_by(Sale.date.desc(), Sale.id.desc()).limit(250).all(),
        "owners": scope_query_to_company(User.query.filter(User.active.is_(True)), User).order_by(User.username).all(),
    }


def _parse_opportunity_form(client_id, opportunity_id=None):
    client = _client_for_company(client_id)
    if not client:
        raise ValueError("El cliente no pertenece a tu empresa o no existe.")
    title = (request.form.get("title") or "").strip()[:180] or f"Oportunidad para {client.name}"
    stage = (request.form.get("stage") or "nuevo").strip().lower()
    if stage not in STAGE_LABELS:
        raise ValueError("La etapa comercial no es válida.")
    try:
        value = round(float(request.form.get("value") or 0), 2)
        probability = float(request.form.get("probability") or 0)
    except (TypeError, ValueError):
        raise ValueError("El valor o la probabilidad no tienen un formato válido.")
    if value < 0 or not 0 <= probability <= 100:
        raise ValueError("El valor debe ser positivo y la probabilidad debe estar entre 0 y 100.")
    quote_id = request.form.get("quote_id", type=int)
    sale_id = request.form.get("sale_id", type=int)
    quote, sale = _resolve_commercial_links(client.id, quote_id, sale_id, opportunity_id)
    if opportunity_id:
        existing_opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first()
        if existing_opportunity and existing_opportunity.sale_id and (
            sale_id != existing_opportunity.sale_id or quote_id != existing_opportunity.quote_id
        ):
            raise ValueError("No se puede desvincular o reemplazar el presupuesto/venta de una operación registrada; conservá la trazabilidad comercial.")
    owner_id = request.form.get("owner_user_id", type=int) or current_user.id
    from app import User
    owner = scope_query_to_company(User.query.filter(User.active.is_(True)), User).filter(User.id == owner_id).first()
    if not owner:
        raise ValueError("El responsable debe ser un usuario activo de tu empresa.")
    expected_close_date = _parse_datetime(request.form.get("expected_close_date"))
    date_raw = (request.form.get("expected_close_date") or "").strip()
    if date_raw and expected_close_date is None:
        raise ValueError("La fecha estimada de cierre no es válida.")
    loss_reason = (request.form.get("loss_reason") or "").strip() or None
    if sale is not None:
        value = float(sale.total_amount or 0)
        sale_is_annulled = str(sale.status or "").strip().lower() in {"anulada", "cancelada", "cancelled", "voided"}
        stage = "perdido" if sale_is_annulled else "ganado"
        probability = 0 if sale_is_annulled else 100
        if sale_is_annulled:
            loss_reason = loss_reason or f"Venta vinculada #{sale.id} anulada."
        else:
            loss_reason = None
    elif stage == "ganado":
        probability = 100
        loss_reason = None
    elif stage == "perdido":
        probability = 0
        if not loss_reason:
            raise ValueError("Para cerrar una oportunidad como Perdida, indicá el motivo.")
    else:
        loss_reason = None
    return {
        "client": client,
        "title": title,
        "stage": stage,
        "status": _stage_status(stage),
        "value": value,
        "probability": probability,
        "expected_close_date": expected_close_date,
        "owner_user_id": owner.id,
        "source": (request.form.get("source") or "").strip()[:60] or None,
        "notes": (request.form.get("notes") or "").strip() or None,
        "quote_id": quote.id if quote else None,
        "sale_id": sale.id if sale else None,
        "loss_reason": loss_reason,
    }


@bp.route("/")
@tenant_required
@ai_pro_crm_required
def index():
    company_id = _company_id()
    open_opps = _opportunity_query().filter(CRMOpportunity.status == "open")
    stats = {
        "open_count": open_opps.count(),
        "pipeline_value": float(db.session.query(func.coalesce(func.sum(CRMOpportunity.value), 0)).filter(
            CRMOpportunity.company_id == company_id, CRMOpportunity.status == "open"
        ).scalar() or 0),
        "weighted_value": float(sum(row.weighted_value for row in open_opps.all())),
        "overdue_activities": _activity_query().filter(
            CRMActivity.status == "pending",
            CRMActivity.due_at.isnot(None),
            CRMActivity.due_at < _now(),
        ).count(),
        "today_activities": _activity_query().filter(
            CRMActivity.status == "pending",
            CRMActivity.due_at.isnot(None),
            CRMActivity.due_at >= _now().replace(hour=0, minute=0, second=0, microsecond=0),
            CRMActivity.due_at < (_now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)),
        ).count(),
    }
    priorities = _activity_query().filter(CRMActivity.status == "pending").order_by(CRMActivity.due_at.asc().nullslast(), CRMActivity.id.desc()).limit(8).all()
    hot = open_opps.order_by(CRMOpportunity.probability.desc(), CRMOpportunity.value.desc()).limit(6).all()
    return render_template("crm/index.html", stats=stats, priorities=priorities, hot=hot, stages=STAGES)


@bp.route("/pipeline")
@tenant_required
@ai_pro_crm_required
def pipeline():
    from app import Client, User

    status_filter = (request.args.get("status") or "open").strip().lower()
    if status_filter not in {"open", "closed", "all"}:
        status_filter = "open"
    stage_filter = (request.args.get("stage") or "").strip().lower()
    if stage_filter not in STAGE_LABELS:
        stage_filter = ""
    owner_filter = request.args.get("owner_id", type=int)
    search = (request.args.get("q") or "").strip()
    query = _opportunity_query()
    if status_filter in {"open", "closed"}:
        query = query.filter(CRMOpportunity.status == status_filter)
    if stage_filter:
        query = query.filter(CRMOpportunity.stage == stage_filter)
    if owner_filter:
        query = query.filter(CRMOpportunity.owner_user_id == owner_filter)
    if search:
        like = f"%{search}%"
        query = query.join(CRMOpportunity.client).filter(or_(CRMOpportunity.title.ilike(like), CRMOpportunity.source.ilike(like), Client.name.ilike(like)))
    opportunities = query.order_by(CRMOpportunity.updated_at.desc()).limit(500).all()
    grouped = {stage: [] for stage, _ in STAGES}
    for opportunity in opportunities:
        grouped.setdefault(opportunity.stage, []).append(opportunity)
    owners = scope_query_to_company(User.query.filter(User.active.is_(True)), User).order_by(User.username).all()
    return render_template(
        "crm/pipeline.html",
        grouped=grouped,
        stages=STAGES,
        status_filter=status_filter,
        stage_filter=stage_filter,
        owner_filter=owner_filter,
        search=search,
        owners=owners,
        result_count=len(opportunities),
    )


@bp.route("/prospects/new", methods=["GET", "POST"])
@tenant_required
@ai_pro_crm_required
def prospect_new():
    from app import Client, db

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:200]
        email = (request.form.get("email") or "").strip().lower() or None
        phone = (request.form.get("phone") or "").strip() or None
        whatsapp = (request.form.get("whatsapp") or "").strip() or None
        if not name:
            flash("El nombre del prospecto es obligatorio.", "danger")
            return render_template("crm/prospect_form.html"), 400
        if email and (len(email) > 120 or email.count("@") != 1):
            flash("El email no tiene un formato válido o supera el máximo permitido.", "danger")
            return render_template("crm/prospect_form.html"), 400
        if phone and (len(phone) > 20 or len(re.sub(r"\D+", "", phone)) < 6):
            flash("El teléfono debe contener al menos seis dígitos y no superar los 20 caracteres.", "danger")
            return render_template("crm/prospect_form.html"), 400
        if whatsapp and (len(whatsapp) > 30 or len(re.sub(r"\D+", "", whatsapp)) < 6):
            flash("El WhatsApp debe contener al menos seis dígitos y no superar los 30 caracteres.", "danger")
            return render_template("crm/prospect_form.html"), 400
        if not any((email, phone, whatsapp)):
            flash("Ingresá al menos un email, teléfono o WhatsApp para poder dar seguimiento.", "danger")
            return render_template("crm/prospect_form.html"), 400
        try:
            value = round(float(request.form.get("value") or 0), 2)
            probability = float(request.form.get("probability") or 10)
        except (TypeError, ValueError):
            flash("El valor o la probabilidad no tienen un formato válido.", "danger")
            return render_template("crm/prospect_form.html"), 400
        if value < 0 or not 0 <= probability <= 100:
            flash("El valor debe ser positivo y la probabilidad debe estar entre 0 y 100.", "danger")
            return render_template("crm/prospect_form.html"), 400
        expected_close_date = _parse_datetime(request.form.get("expected_close_date"))
        if (request.form.get("expected_close_date") or "").strip() and expected_close_date is None:
            flash("La fecha estimada de cierre no es válida.", "danger")
            return render_template("crm/prospect_form.html"), 400

        def phone_digits(raw):
            return re.sub(r"\D+", "", str(raw or ""))

        phone_digits_inputs = {phone_digits(value) for value in (phone, whatsapp) if value}
        candidate_query = scope_query_to_company(Client.query, Client)
        match_conditions = []
        if email:
            match_conditions.append(func.lower(Client.email) == email)
        for digits in phone_digits_inputs:
            if len(digits) >= 4:
                tail = digits[-4:]
                match_conditions.extend((Client.phone.ilike(f"%{tail}%"), Client.whatsapp.ilike(f"%{tail}%")))
        if match_conditions:
            candidates = candidate_query.filter(or_(*match_conditions)).limit(100).all()
        else:
            candidates = []
        exact_matches = {}
        for existing in candidates:
            same_email = bool(email and (existing.email or "").strip().lower() == email)
            input_phones = {phone_digits(v) for v in (phone, whatsapp) if v}
            existing_phones = {phone_digits(v) for v in (existing.phone, existing.whatsapp) if v}
            same_phone = bool(input_phones.intersection(existing_phones))
            if same_email or same_phone:
                exact_matches[existing.id] = existing
        if len(exact_matches) > 1:
            flash("Los datos coinciden con más de un cliente. Revisá la ficha existente antes de crear otra oportunidad.", "danger")
            return render_template("crm/prospect_form.html"), 409

        now = _now()
        client = next(iter(exact_matches.values()), None)
        created_client = client is None
        if client is None:
            client = Client(
                company_id=_company_id(),
                name=name,
                email=email,
                phone=phone,
                whatsapp=whatsapp,
                address=(request.form.get("address") or "").strip() or None,
                city=(request.form.get("city") or "").strip()[:100] or None,
                province=(request.form.get("province") or "").strip()[:120] or None,
                postal_code=(request.form.get("postal_code") or "").strip()[:20] or None,
                notes=(request.form.get("client_notes") or "").strip() or "Contacto inicial registrado desde CRM.",
                active=True,
            )
            db.session.add(client)
            db.session.flush()
        else:
            if not client.active:
                client.active = True
            # Enrichment fills blanks only; never silently overwrites existing contact data.
            for field, value_in in (
                ("email", email),
                ("phone", phone),
                ("whatsapp", whatsapp),
                ("address", (request.form.get("address") or "").strip() or None),
                ("city", (request.form.get("city") or "").strip()[:100] or None),
                ("province", (request.form.get("province") or "").strip()[:120] or None),
                ("postal_code", (request.form.get("postal_code") or "").strip()[:20] or None),
            ):
                if not getattr(client, field, None) and value_in:
                    setattr(client, field, value_in)

        opportunity = CRMOpportunity(
            company_id=_company_id(),
            client_id=client.id,
            title=(request.form.get("opportunity_title") or "").strip()[:180] or f"Oportunidad para {client.name}",
            stage="nuevo",
            status="open",
            value=value,
            probability=probability,
            expected_close_date=expected_close_date,
            owner_user_id=current_user.id,
            source=(request.form.get("source") or "CRM").strip()[:60] or "CRM",
            notes=(request.form.get("notes") or "").strip() or None,
            created_at=now,
            updated_at=now,
        )
        db.session.add(opportunity)
        followup_subject = (request.form.get("followup_subject") or "").strip()
        followup_due_raw = (request.form.get("followup_due_at") or "").strip()
        followup_due = _parse_datetime(followup_due_raw)
        if followup_due_raw and followup_due is None:
            db.session.rollback()
            flash("La fecha del primer seguimiento no es válida.", "danger")
            return render_template("crm/prospect_form.html"), 400
        if followup_subject or followup_due:
            db.session.add(CRMActivity(
                company_id=_company_id(),
                client_id=client.id,
                opportunity=opportunity,
                user_id=current_user.id,
                type="seguimiento",
                status="pending",
                subject=followup_subject[:180] or "Primer seguimiento al prospecto",
                description="Primer seguimiento creado al registrar el prospecto.",
                due_at=followup_due,
                created_at=now,
                updated_at=now,
            ))
        db.session.commit()
        flash(
            ("Cliente existente reutilizado; " if not created_client else "Prospecto creado; ")
            + "oportunidad y seguimiento guardados.",
            "success",
        )
        return redirect(url_for("crm.opportunity_detail", opportunity_id=opportunity.id))
    return render_template("crm/prospect_form.html")


@bp.route("/opportunities/new", methods=["GET", "POST"])
@tenant_required
@ai_pro_crm_required
def opportunity_new():
    from app import db

    options = _opportunity_form_options()
    if request.method == "POST":
        try:
            values = _parse_opportunity_form(request.form.get("client_id", type=int))
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("crm/opportunity_form.html", **options, stages=STAGES, opportunity=None, selected_client_id=request.form.get("client_id", type=int)), 400
        now = _now()
        opportunity = CRMOpportunity(
            company_id=_company_id(),
            client_id=values["client"].id,
            title=values["title"],
            stage=values["stage"],
            status=values["status"],
            value=values["value"],
            probability=values["probability"],
            expected_close_date=values["expected_close_date"],
            owner_user_id=values["owner_user_id"],
            source=values["source"],
            quote_id=values["quote_id"],
            sale_id=values["sale_id"],
            notes=values["notes"],
            loss_reason=values["loss_reason"],
            created_at=now,
            updated_at=now,
        )
        db.session.add(opportunity)
        if opportunity.sale_id:
            opportunity.stage = "ganado"
            opportunity.status = "closed"
        db.session.commit()
        flash("Oportunidad creada y vinculada a sus registros comerciales.", "success")
        return redirect(url_for("crm.opportunity_detail", opportunity_id=opportunity.id))
    return render_template("crm/opportunity_form.html", **options, stages=STAGES, opportunity=None, selected_client_id=request.args.get("client_id", type=int))


@bp.route("/opportunities/<int:opportunity_id>/edit", methods=["GET", "POST"])
@tenant_required
@ai_pro_crm_required
def opportunity_edit(opportunity_id):
    from app import db

    opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first_or_404()
    options = _opportunity_form_options()
    if request.method == "POST":
        try:
            values = _parse_opportunity_form(request.form.get("client_id", type=int), opportunity_id=opportunity.id)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("crm/opportunity_form.html", **options, stages=STAGES, opportunity=opportunity), 400
        previous_loss_reason = (opportunity.loss_reason or "").strip()
        if previous_loss_reason and values["loss_reason"] != previous_loss_reason:
            _archive_loss_reason(
                opportunity,
                notes_override=values["notes"] if values["notes"] is not None else opportunity.notes,
            )
        else:
            opportunity.notes = values["notes"] or opportunity.notes
        opportunity.client_id = values["client"].id
        opportunity.title = values["title"]
        opportunity.stage = values["stage"]
        opportunity.status = values["status"]
        opportunity.value = values["value"]
        opportunity.probability = values["probability"]
        opportunity.expected_close_date = values["expected_close_date"]
        opportunity.owner_user_id = values["owner_user_id"]
        opportunity.source = values["source"]
        opportunity.loss_reason = values["loss_reason"]
        opportunity.quote_id = values["quote_id"]
        opportunity.sale_id = values["sale_id"]
        if opportunity.sale_id:
            opportunity.stage = "ganado"
            opportunity.status = "closed"
        opportunity.updated_at = _now()
        db.session.commit()
        flash("Oportunidad actualizada.", "success")
        return redirect(url_for("crm.opportunity_detail", opportunity_id=opportunity.id))
    return render_template("crm/opportunity_form.html", **options, stages=STAGES, opportunity=opportunity)


@bp.route("/opportunities/<int:opportunity_id>")
@tenant_required
@ai_pro_crm_required
def opportunity_detail(opportunity_id):
    opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first_or_404()
    activities = _activity_query().filter(CRMActivity.opportunity_id == opportunity.id).order_by(CRMActivity.due_at.desc().nullslast(), CRMActivity.id.desc()).all()
    return render_template("crm/opportunity_detail.html", opportunity=opportunity, activities=activities, activity_types=ACTIVITY_TYPES, stages=STAGES)


@bp.route("/opportunities/<int:opportunity_id>/stage", methods=["POST"])
@tenant_required
@ai_pro_crm_required
def opportunity_stage(opportunity_id):
    opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first_or_404()
    stage = request.form.get("stage")
    if stage not in STAGE_LABELS:
        abort(400)
    from app import Sale

    loss_reason = (request.form.get("loss_reason") or "").strip() or None
    linked_sale = None
    if opportunity.sale_id:
        linked_sale = scope_query_to_company(Sale.query, Sale).filter(Sale.id == opportunity.sale_id).first()
        if linked_sale is not None:
            sale_is_annulled = str(linked_sale.status or "").strip().lower() in {"anulada", "cancelada", "cancelled", "voided"}
            required_stage = "perdido" if sale_is_annulled else "ganado"
            if stage != required_stage:
                flash("La etapa debe reflejar el resultado real de la venta vinculada. Para cambiarla, corregí primero el estado de la venta.", "warning")
                return redirect(request.referrer or url_for("crm.opportunity_detail", opportunity_id=opportunity.id))
            opportunity.value = linked_sale.total_amount or opportunity.value
            if sale_is_annulled:
                loss_reason = loss_reason or f"Venta vinculada #{linked_sale.id} anulada."

    if stage == "perdido" and not loss_reason and linked_sale is None:
        flash("Para cerrar una oportunidad como Perdida, indicá el motivo.", "danger")
        return redirect(request.referrer or url_for("crm.opportunity_detail", opportunity_id=opportunity.id))

    if stage == "perdido":
        opportunity.loss_reason = loss_reason
        opportunity.probability = 0
    else:
        if opportunity.loss_reason:
            _archive_loss_reason(opportunity)
        opportunity.loss_reason = None
        if stage == "ganado":
            opportunity.probability = 100

    opportunity.stage = stage
    opportunity.status = _stage_status(stage)
    opportunity.updated_at = _now()
    db.session.commit()
    flash("Etapa actualizada.", "success")
    return redirect(request.referrer or url_for("crm.pipeline"))


@bp.route("/activities", methods=["GET", "POST"])
@tenant_required
@ai_pro_crm_required
def activities():
    from app import Client

    if request.method == "POST":
        client_id = request.form.get("client_id", type=int)
        client = _client_for_company(client_id)
        if not client:
            flash("El cliente no pertenece a tu empresa o no existe.", "danger")
            return redirect(url_for("crm.activities"))
        opportunity_id = request.form.get("opportunity_id", type=int)
        opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first() if opportunity_id else None
        if opportunity_id and opportunity is None:
            flash("La oportunidad no pertenece a tu empresa o ya no existe.", "danger")
            return redirect(url_for("crm.activities"))
        if opportunity and opportunity.client_id != client.id:
            flash("La oportunidad no corresponde al cliente seleccionado.", "danger")
            return redirect(url_for("crm.activities"))
        activity_type = request.form.get("type") if request.form.get("type") in dict(ACTIVITY_TYPES) else "tarea"
        due_raw = (request.form.get("due_at") or "").strip()
        due_at = _parse_datetime(due_raw)
        if due_raw and due_at is None:
            flash("La fecha del seguimiento no tiene un formato válido.", "danger")
            return redirect(url_for("crm.activities", client_id=client.id, opportunity_id=opportunity.id if opportunity else None))
        now = _now()
        activity = CRMActivity(
            company_id=_company_id(), client_id=client.id, opportunity_id=opportunity.id if opportunity else None,
            user_id=current_user.id, type=activity_type, status="pending",
            subject=(request.form.get("subject") or "").strip()[:180] or "Seguimiento comercial",
            description=(request.form.get("description") or "").strip() or None,
            due_at=due_at, created_at=now, updated_at=now,
        )
        db.session.add(activity)
        db.session.commit()
        flash("Seguimiento creado.", "success")
        return redirect(url_for("crm.activities", client_id=client.id, opportunity_id=opportunity.id if opportunity else None))

    status_filter = (request.args.get("status") or "pending").strip().lower()
    if status_filter not in {"pending", "completed", "all"}:
        status_filter = "pending"
    activity_query = _activity_query()
    if status_filter in {"pending", "completed"}:
        activity_query = activity_query.filter(CRMActivity.status == status_filter)
    activities = activity_query.order_by(
        CRMActivity.due_at.asc().nullslast(),
        CRMActivity.id.desc(),
    ).limit(250).all()
    clients = scope_query_to_company(Client.query.filter(Client.active.is_(True)), Client).order_by(Client.name).all()
    opportunities = _opportunity_query().filter(CRMOpportunity.status == "open").order_by(CRMOpportunity.updated_at.desc()).all()
    return render_template(
        "crm/activities.html",
        activities=activities,
        clients=clients,
        opportunities=opportunities,
        activity_types=ACTIVITY_TYPES,
        selected_client_id=request.args.get("client_id", type=int),
        selected_opportunity_id=request.args.get("opportunity_id", type=int),
        status_filter=status_filter,
        now=_now(),
    )


@bp.route("/activities/<int:activity_id>/edit", methods=["GET", "POST"])
@tenant_required
@ai_pro_crm_required
def activity_edit(activity_id):
    from app import Client, db

    activity = _activity_query().filter(CRMActivity.id == activity_id).first_or_404()
    if request.method == "POST":
        client_id = request.form.get("client_id", type=int)
        client = _client_for_company(client_id)
        opportunity_id = request.form.get("opportunity_id", type=int)
        opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first() if opportunity_id else None
        if not client or (opportunity_id and not opportunity):
            flash("El cliente o la oportunidad seleccionados no pertenecen a tu empresa.", "danger")
            return redirect(url_for("crm.activity_edit", activity_id=activity.id))
        if opportunity and opportunity.client_id != client.id:
            flash("La oportunidad debe pertenecer al cliente seleccionado.", "danger")
            return redirect(url_for("crm.activity_edit", activity_id=activity.id))
        activity_type = request.form.get("type") if request.form.get("type") in dict(ACTIVITY_TYPES) else "tarea"
        due_raw = (request.form.get("due_at") or "").strip()
        due_at = _parse_datetime(due_raw)
        if due_raw and due_at is None:
            flash("La fecha del seguimiento no tiene un formato válido.", "danger")
            return redirect(url_for("crm.activity_edit", activity_id=activity.id))
        subject = (request.form.get("subject") or "").strip()
        if not subject:
            flash("El asunto del seguimiento es obligatorio.", "danger")
            return redirect(url_for("crm.activity_edit", activity_id=activity.id))
        activity.client_id = client.id
        activity.opportunity_id = opportunity.id if opportunity else None
        activity.type = activity_type
        activity.subject = subject[:180]
        activity.description = (request.form.get("description") or "").strip() or None
        activity.due_at = due_at
        activity.updated_at = _now()
        db.session.commit()
        flash("Seguimiento actualizado.", "success")
        return redirect(url_for("crm.activities", status="all"))

    clients = scope_query_to_company(Client.query.filter(Client.active.is_(True)), Client).order_by(Client.name).all()
    opportunities_query = _opportunity_query()
    if activity.opportunity_id:
        opportunities_query = opportunities_query.filter(
            or_(CRMOpportunity.status == "open", CRMOpportunity.id == activity.opportunity_id)
        )
    else:
        opportunities_query = opportunities_query.filter(CRMOpportunity.status == "open")
    opportunities = opportunities_query.order_by(CRMOpportunity.updated_at.desc()).all()
    return render_template(
        "crm/activity_form.html",
        activity=activity,
        clients=clients,
        opportunities=opportunities,
        activity_types=ACTIVITY_TYPES,
    )


@bp.route("/activities/<int:activity_id>/delete", methods=["POST"])
@tenant_required
@ai_pro_crm_required
def activity_delete(activity_id):
    activity = _activity_query().filter(CRMActivity.id == activity_id).first_or_404()
    db.session.delete(activity)
    db.session.commit()
    flash("Seguimiento eliminado.", "success")
    return redirect(url_for("crm.activities", status="all"))


@bp.route("/activities/<int:activity_id>/complete", methods=["POST"])
@tenant_required
@ai_pro_crm_required
def activity_complete(activity_id):
    activity = _activity_query().filter(CRMActivity.id == activity_id).first_or_404()
    now = _now()
    activity.status = "completed"
    activity.completed_at = now
    activity.updated_at = now
    db.session.commit()
    flash("Actividad completada.", "success")
    return redirect(request.referrer or url_for("crm.activities"))


@bp.route("/clients/<int:client_id>/contact/<channel>", methods=["POST"])
@tenant_required
@ai_pro_crm_required
def client_contact(client_id, channel):
    """Open a real communication channel and record a follow-up task without pretending it was sent."""
    from app import Client, db

    client = _client_for_company(client_id)
    if not client:
        abort(404)
    channel = (channel or "").strip().lower()
    if channel == "whatsapp":
        raw_phone = str(client.whatsapp or client.phone or "").strip()
        phone = re.sub(r"\D+", "", raw_phone)
        if not phone:
            flash("El cliente no tiene un teléfono válido para WhatsApp.", "warning")
            return redirect(url_for("crm.client_360", client_id=client.id))
        destination = f"https://wa.me/{phone}"
        activity_type = "whatsapp"
        subject = f"Contactar por WhatsApp: {client.name}"
    elif channel == "email":
        email = str(client.email or "").strip()
        if not email or "@" not in email:
            flash("El cliente no tiene un email válido.", "warning")
            return redirect(url_for("crm.client_360", client_id=client.id))
        email_subject = urlencode({"subject": f"Seguimiento comercial - {client.name}"})
        destination = f"mailto:{email}?{email_subject}"
        activity_type = "email"
        subject = f"Contactar por email: {client.name}"
    else:
        abort(400)

    now = _now()
    activity = CRMActivity(
        company_id=_company_id(),
        client_id=client.id,
        user_id=current_user.id,
        type=activity_type,
        status="pending",
        subject=subject[:180],
        description="Contacto abierto desde la ficha CRM. Marcá el seguimiento como completado después de enviar el mensaje; el CRM no puede confirmar por sí solo que se haya enviado.",
        created_at=now,
        updated_at=now,
    )
    db.session.add(activity)
    db.session.commit()
    flash("Canal abierto y seguimiento registrado. Confirmá el envío y completá la actividad luego.", "info")
    return redirect(destination)


@bp.route("/clients/<int:client_id>")
@tenant_required
@ai_pro_crm_required
def client_360(client_id):
    from app import Client, Quote, Sale

    client = _client_for_company(client_id)
    if not client:
        abort(404)

    all_sales_query = scope_query_to_company(Sale.query.filter(Sale.client_id == client.id), Sale)
    all_sales_query = all_sales_query.filter(Sale.status.notin_(["cancelada", "anulada", "cancelled", "voided"]))
    sales_count = all_sales_query.count()
    total_spent = float(
        all_sales_query.with_entities(func.coalesce(func.sum(Sale.total_amount), 0)).scalar() or 0
    )
    sales = all_sales_query.order_by(Sale.date.desc()).limit(12).all()

    all_quotes_query = scope_query_to_company(Quote.query.filter(Quote.client_id == client.id), Quote)
    quotes_count = all_quotes_query.count()
    quotes = all_quotes_query.order_by(Quote.date.desc()).limit(12).all()

    opportunities = _opportunity_query().filter(CRMOpportunity.client_id == client.id).order_by(CRMOpportunity.updated_at.desc()).all()
    activities = _activity_query().filter(CRMActivity.client_id == client.id).order_by(CRMActivity.created_at.desc()).limit(50).all()

    timeline = []
    for item in activities:
        occurred_at = item.completed_at if item.status == "completed" and item.completed_at else item.created_at
        timeline.append({
            "date": occurred_at,
            "title": item.subject,
            "subtitle": f"{dict(ACTIVITY_TYPES).get(item.type, item.type)} · {'Completada' if item.status == 'completed' else 'Pendiente'}",
            "description": item.description,
            "color": "primary",
            "url": url_for("crm.opportunity_detail", opportunity_id=item.opportunity_id) if item.opportunity_id else None,
        })
    for item in quotes:
        timeline.append({
            "date": item.date,
            "title": f"Presupuesto {getattr(item, 'number', None) or '#' + str(item.id)}",
            "subtitle": f"ARS {float(item.total_amount or 0):.2f} · {item.status or 'Sin estado'}",
            "description": getattr(item, "notes", None) or getattr(item, "observations", None),
            "color": "info",
            "url": url_for("quotes.view_quote", quote_id=item.id),
        })
    for item in sales:
        timeline.append({
            "date": item.date,
            "title": f"Venta #{item.id}",
            "subtitle": f"ARS {float(item.total_amount or 0):.2f} · {item.status or 'Registrada'}",
            "description": item.note,
            "color": "success",
            "url": url_for("sales.view_sale", sale_id=item.id),
        })
    timeline.sort(key=lambda event: event["date"] or datetime.min, reverse=True)
    timeline = timeline[:50]

    return render_template(
        "crm/client_360.html",
        client=client,
        sales=sales,
        sales_count=sales_count,
        quotes=quotes,
        quotes_count=quotes_count,
        opportunities=opportunities,
        activities=activities,
        timeline=timeline,
        total_spent=total_spent,
        stages=STAGES,
        activity_types=ACTIVITY_TYPES,
    )


@bp.route("/clients")
@tenant_required
@ai_pro_crm_required
def clients():
    from app import Client
    search = (request.args.get("search") or "").strip()
    query = scope_query_to_company(Client.query.filter(Client.active.is_(True)), Client)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Client.name.ilike(like), Client.email.ilike(like), Client.phone.ilike(like), Client.whatsapp.ilike(like)))
    clients = query.order_by(Client.name).all()
    return render_template("crm/clients.html", clients=clients, search=search)
