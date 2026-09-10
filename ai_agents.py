"""Tenant-facing dashboard for the existing StockARmobile AI capabilities."""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import tenant_required
from services.ai_agent.config_service import (
    BUSINESS_AGENT_NAME,
    VENDOR_AGENT_NAME,
    ensure_default_agents,
    get_ai_preferences,
)
from services.ai_agent.usage_service import AI_PLANS, AGENT_LABELS, can_use_ai, current_plan, usage_snapshot
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation
from stockarmobile.decorators import company_admin_required
from stockarmobile.permissions import can_access_ai
from services.ai_agent.campaign_service import CampaignService


bp = Blueprint("ai_agents", __name__, url_prefix="/agentes-ia")


@bp.before_request
def _require_ai_access():
    if not can_access_ai(current_user):
        flash("Tu usuario no tiene habilitado el acceso a Agentes IA. Pedile al administrador de la empresa que lo active desde Mi Empresa.", "warning")
        return redirect(url_for("dashboard.index"))


def _context():
    from app import Client, Company, Product, Quote, Sale, SaleItem
    from services.ai_agent.subscription_service import AISubscriptionService

    company_id = current_user.company_id
    company = Company.query.get(company_id)
    agents = ensure_default_agents(company_id)
    preferences = get_ai_preferences(company)
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_start = (month_start - timedelta(days=1)).replace(day=1)

    conversations = Conversation.query.filter(Conversation.company_id == company_id, Conversation.created_at >= month_start).count()
    clients_attended = db.session.query(Conversation.external_conversation_id).filter(Conversation.company_id == company_id, Conversation.created_at >= month_start, Conversation.external_conversation_id.isnot(None)).distinct().count()
    quotes = Quote.query.filter(Quote.company_id == company_id, Quote.created_at >= month_start).count()
    sales_month = Sale.query.filter(Sale.company_id == company_id, Sale.created_at >= month_start, Sale.status.notin_(["cancelada", "anulada"]))
    sales_previous = Sale.query.filter(Sale.company_id == company_id, Sale.created_at >= previous_start, Sale.created_at < month_start, Sale.status.notin_(["cancelada", "anulada"]))
    sales_total = float(sales_month.with_entities(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    previous_total = float(sales_previous.with_entities(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    sales_change = None if previous_total == 0 else round(((sales_total - previous_total) / previous_total) * 100, 1)
    critical_stock = Product.query.filter(Product.company_id == company_id, Product.active.is_(True), Product.stock <= Product.min_stock).count()
    sold_product_ids = db.session.query(SaleItem.product_id).join(Sale).filter(Sale.company_id == company_id, Sale.created_at >= now - timedelta(days=90), Sale.status.notin_(["cancelada", "anulada"])).distinct()
    low_rotation = Product.query.filter(Product.company_id == company_id, Product.active.is_(True), ~Product.id.in_(sold_product_ids)).count()
    ai_status = AISubscriptionService.get_status(company)
    ai_plan = current_plan(company)
    ai_usage = usage_snapshot(company_id)
    agent_access = {key: can_use_ai(company, key) for key in AGENT_LABELS}
    invoice_access = can_use_ai(company, "facturas")

    any_chat_agent = any(access.allowed for access in agent_access.values())
    default_chat_agent = "asistente"
    if not agent_access[default_chat_agent].allowed:
        for candidate in ("vendedor", "analista", "marketing"):
            if agent_access[candidate].allowed:
                default_chat_agent = candidate
                break

    return {"company": company, "agents": agents, "preferences": preferences, "metrics": {"conversations": conversations, "clients_attended": clients_attended, "quotes": quotes, "sales": int(sales_month.count())}, "analyst": {"sales_change": sales_change, "critical_stock": critical_stock, "low_rotation": low_rotation, "opportunities": None}, "ai_status": ai_status, "ai_plans": AI_PLANS, "agent_labels": AGENT_LABELS, "ai_plan": ai_plan, "ai_usage": ai_usage, "agent_access": agent_access, "invoice_access": invoice_access, "any_chat_agent": any_chat_agent, "default_chat_agent": default_chat_agent, "plan_url": url_for("ai_agents.agent", agent="planes"), "ai_checkout_url": url_for("company_billing.create_ai_subscription_checkout"), "config_url": url_for("ai_admin.index") if current_user.role == "admin" else None, "chat_url": url_for("dashboard.ai_agent_chat")}


def _campaign_rows(company_id: int):
    from app import Campaign
    return Campaign.query.filter_by(company_id=company_id).order_by(Campaign.updated_at.desc(), Campaign.id.desc()).all()


def _campaign_summary(company_id: int):
    rows = _campaign_rows(company_id)
    return {status: sum(1 for row in rows if row.status == status) for status in ("BORRADOR", "PENDIENTE_APROBACION", "APROBADA")}


@bp.get("/")
@tenant_required
def index():
    return render_template("ai_agents/index.html", view="dashboard", **_context())


@bp.get("/campanas")
@tenant_required
def campaigns():
    return render_template("ai_agents/campaigns.html", campaigns=_campaign_rows(current_user.company_id), campaign_summary=_campaign_summary(current_user.company_id), **_context())


@bp.get("/campanas/<int:campaign_id>")
@tenant_required
def campaign_detail(campaign_id):
    campaign = CampaignService._campaign(current_user.company_id, campaign_id)
    if campaign is None:
        from flask import abort
        abort(404)
    return render_template("ai_agents/campaign_detail.html", campaign=campaign, **_context())


@bp.post("/campanas/<int:campaign_id>/edit")
@tenant_required
def campaign_edit(campaign_id):
    try:
        CampaignService.update_draft(company_id=current_user.company_id, campaign_id=campaign_id, title=request.form.get("title", ""), objective=request.form.get("objective", ""), content=request.form.get("content", ""), user_id=current_user.id)
        db.session.commit()
        flash("Campaña actualizada.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.post("/campanas/<int:campaign_id>/transition")
@company_admin_required
def campaign_transition(campaign_id):
    target_status = request.form.get("status", "")
    try:
        CampaignService.transition(company_id=current_user.company_id, campaign_id=campaign_id, target_status=target_status, user_id=current_user.id)
        db.session.commit()
        flash("Estado de campaña actualizado.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.get("/<agent>")
@tenant_required
def agent(agent):
    if agent not in {"vendedor", "asistente", "analista", "marketing", "planes"}:
        abort(404)
    if agent != "planes":
        access = _context()["agent_access"][agent]
        if not access.allowed:
            return render_template("ai_agents/index.html", view="locked", locked_agent=agent, locked_reason=access.reason, **_context())
    return render_template("ai_agents/index.html", view=agent, **_context())
