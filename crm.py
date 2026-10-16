"""CRM comercial para empresas tenant de StockArMobile.

IMPORTANTE: no contiene ni reutiliza entidades del CRM de SuperAdmin.
"""
from datetime import datetime, timedelta

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
    owner_id = request.form.get("owner_user_id", type=int) or current_user.id
    from app import User
    owner = scope_query_to_company(User.query.filter(User.active.is_(True)), User).filter(User.id == owner_id).first()
    if not owner:
        raise ValueError("El responsable debe ser un usuario activo de tu empresa.")
    expected_close_date = _parse_datetime(request.form.get("expected_close_date"))
    date_raw = (request.form.get("expected_close_date") or "").strip()
    if date_raw and expected_close_date is None:
        raise ValueError("La fecha estimada de cierre no es válida.")
    if sale is not None:
        stage = "ganado"
        value = float(sale.total_amount or 0)
        probability = 100
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
    opportunities = _opportunity_query().filter(CRMOpportunity.status == "open").order_by(CRMOpportunity.updated_at.desc()).all()
    grouped = {stage: [] for stage, _ in STAGES if stage not in {"ganado", "perdido"}}
    for opportunity in opportunities:
        grouped.setdefault(opportunity.stage, []).append(opportunity)
    return render_template("crm/pipeline.html", grouped=grouped, stages=STAGES)


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
            return render_template("crm/opportunity_form.html", **options, stages=STAGES, opportunity=None), 400
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
    return render_template("crm/opportunity_form.html", **options, stages=STAGES, opportunity=None)


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
        opportunity.client_id = values["client"].id
        opportunity.title = values["title"]
        opportunity.stage = values["stage"]
        opportunity.status = values["status"]
        opportunity.value = values["value"]
        opportunity.probability = values["probability"]
        opportunity.expected_close_date = values["expected_close_date"]
        opportunity.owner_user_id = values["owner_user_id"]
        opportunity.source = values["source"]
        opportunity.notes = values["notes"]
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
    opportunity.stage = stage
    opportunity.status = "closed" if stage in {"ganado", "perdido"} else "open"
    opportunity.updated_at = _now()
    db.session.commit()
    flash("Etapa actualizada.", "success")
    return redirect(request.referrer or url_for("crm.pipeline"))


@bp.route("/activities", methods=["GET", "POST"])
@tenant_required
@ai_pro_crm_required
def activities():
    if request.method == "POST":
        client_id = request.form.get("client_id", type=int)
        client = _client_for_company(client_id)
        if not client:
            flash("El cliente no pertenece a tu empresa o no existe.", "danger")
            return redirect(url_for("crm.activities"))
        opportunity_id = request.form.get("opportunity_id", type=int)
        opportunity = _opportunity_query().filter(CRMOpportunity.id == opportunity_id).first() if opportunity_id else None
        if opportunity and opportunity.client_id != client.id:
            flash("La oportunidad no corresponde al cliente seleccionado.", "danger")
            return redirect(url_for("crm.activities"))
        activity_type = request.form.get("type") if request.form.get("type") in dict(ACTIVITY_TYPES) else "tarea"
        now = _now()
        activity = CRMActivity(
            company_id=_company_id(), client_id=client.id, opportunity_id=opportunity.id if opportunity else None,
            user_id=current_user.id, type=activity_type, status="pending",
            subject=(request.form.get("subject") or "").strip() or "Seguimiento comercial",
            description=(request.form.get("description") or "").strip() or None,
            due_at=_parse_datetime(request.form.get("due_at")), created_at=now, updated_at=now,
        )
        db.session.add(activity)
        db.session.commit()
        flash("Actividad creada.", "success")
        return redirect(url_for("crm.activities"))
    activities = _activity_query().order_by(CRMActivity.status.asc(), CRMActivity.due_at.asc().nullslast(), CRMActivity.id.desc()).limit(100).all()
    from app import Client
    clients = scope_query_to_company(Client.query.filter(Client.active.is_(True)), Client).order_by(Client.name).all()
    opportunities = _opportunity_query().filter(CRMOpportunity.status == "open").order_by(CRMOpportunity.updated_at.desc()).all()
    return render_template("crm/activities.html", activities=activities, clients=clients, opportunities=opportunities, activity_types=ACTIVITY_TYPES)


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


@bp.route("/clients/<int:client_id>")
@tenant_required
@ai_pro_crm_required
def client_360(client_id):
    from app import Client, Quote, Sale
    client = _client_for_company(client_id)
    if not client:
        abort(404)
    sales = scope_query_to_company(Sale.query.filter(Sale.client_id == client.id), Sale).order_by(Sale.date.desc()).limit(12).all()
    quotes = scope_query_to_company(Quote.query.filter(Quote.client_id == client.id), Quote).order_by(Quote.date.desc()).limit(12).all()
    opportunities = _opportunity_query().filter(CRMOpportunity.client_id == client.id).order_by(CRMOpportunity.updated_at.desc()).all()
    activities = _activity_query().filter(CRMActivity.client_id == client.id).order_by(CRMActivity.created_at.desc()).limit(20).all()
    total_spent = float(sum(float(s.total_amount or 0) for s in sales))
    return render_template("crm/client_360.html", client=client, sales=sales, quotes=quotes, opportunities=opportunities, activities=activities, total_spent=total_spent, stages=STAGES, activity_types=ACTIVITY_TYPES)


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
