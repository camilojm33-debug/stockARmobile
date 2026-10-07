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
    from app import Client
    clients = scope_query_to_company(Client.query.filter(Client.active.is_(True)), Client).order_by(Client.name).all()
    if request.method == "POST":
        client_id = request.form.get("client_id", type=int)
        client = _client_for_company(client_id)
        if not client:
            flash("El cliente no pertenece a tu empresa o no existe.", "danger")
            return redirect(url_for("crm.opportunity_new"))
        try:
            value = max(0, float(request.form.get("value") or 0))
            probability = min(100, max(0, float(request.form.get("probability") or 0)))
        except (TypeError, ValueError):
            flash("Valor o probabilidad inválidos.", "danger")
            return redirect(url_for("crm.opportunity_new"))
        now = _now()
        opportunity = CRMOpportunity(
            company_id=_company_id(), client_id=client.id,
            title=(request.form.get("title") or "").strip() or f"Oportunidad para {client.name}",
            stage=request.form.get("stage") if request.form.get("stage") in STAGE_LABELS else "nuevo",
            status="open", value=value, probability=probability,
            expected_close_date=_parse_datetime(request.form.get("expected_close_date")),
            owner_user_id=current_user.id, source=(request.form.get("source") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
            created_at=now, updated_at=now,
        )
        db.session.add(opportunity)
        db.session.commit()
        flash("Oportunidad creada.", "success")
        return redirect(url_for("crm.opportunity_detail", opportunity_id=opportunity.id))
    return render_template("crm/opportunity_form.html", clients=clients, stages=STAGES, opportunity=None)


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
