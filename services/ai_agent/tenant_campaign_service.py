"""Tenant-scoped Marketing IA delivery engine.

This module is deliberately separate from the SuperAdmin acquisition CRM.
It reuses the existing SMTP configuration and WhatsApp Cloud transport while
keeping every query, recipient and attribution strictly tenant scoped.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive

CHANNELS = {"whatsapp"}
SENDABLE_STATUSES = {"EN_PREPARACION"}
MAX_RECIPIENTS_PER_CYCLE = 50
DELIVERY_RETRY_DELAY = timedelta(minutes=15)
ATTRIBUTION_DAYS = 30
_AUTO_DISPATCH_WORKER_LOCK = threading.Lock()
_AUTO_DISPATCH_WORKER_STARTED = False


def _now():
    return utcnow_naive()


def _is_retryable_delivery_error(detail: str) -> bool:
    message = str(detail or "").lower()
    status_match = re.search(r"http\s+(\d{3})", message)
    if status_match:
        return int(status_match.group(1)) == 429
    # Retry only explicit SMTP temporary rejection responses. Network timeouts
    # are ambiguous: the provider may have accepted the message before the
    # connection failed, so retrying could create a duplicate delivery.
    return re.search(r"\b(?:421|450|451|452)\b", message) is not None


def _token():
    return secrets.token_urlsafe(48)


def _json(campaign):
    try:
        value = json.loads(campaign.system_data_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _shared_campaign_whatsapp():
    """Resolve the single StockARmobile Vendedor 24/7 number used by tenant CRM campaigns.

    Tenant client lists and campaign records remain isolated by company_id. Only
    the outbound WhatsApp sender is shared; a tenant's own connected WhatsApp
    number is deliberately not used for these CRM campaigns.
    """
    from services.ai_agent.config_service import get_whatsapp_connection
    from services.saas_commercial_whatsapp import get_commercial_company

    sender_company = get_commercial_company()
    if sender_company is None:
        return None, {}
    return sender_company, get_whatsapp_connection(sender_company)


def resolve_shared_campaign_reply(*, phone_number_id: str, sender: str, context_message_id: str = ""):
    """Resolve a platform-sender inbound reply to one tenant campaign, without cross-tenant guessing.

    A quoted/replied-to provider message ID is authoritative. Without it, the
    fallback is allowed only when the customer has recent WhatsApp campaign
    history in exactly one tenant. Ambiguous cross-tenant contacts stay in the
    SuperAdmin inbox instead of leaking a conversation to the wrong business.
    """
    from services.ai_agent.config_service import get_whatsapp_connection
    from services.saas_commercial_whatsapp import get_commercial_company

    platform_company = get_commercial_company()
    if platform_company is None:
        return None
    platform_connection = get_whatsapp_connection(platform_company)
    if str(platform_connection.get("phone_number_id") or "").strip() != str(phone_number_id or "").strip():
        return None

    query = TenantCampaignRecipient.query.filter(
        TenantCampaignRecipient.channel == "whatsapp",
        TenantCampaignRecipient.status == "sent",
        TenantCampaignRecipient.provider_message_id.isnot(None),
    )
    referenced_id = str(context_message_id or "").strip()
    if referenced_id:
        matches = query.filter(TenantCampaignRecipient.provider_message_id == referenced_id).limit(2).all()
        return matches[0] if len(matches) == 1 else None

    from services.whatsapp_share_service import normalize_whatsapp_number
    normalized_sender = normalize_whatsapp_number(sender)
    if not normalized_sender:
        return None
    cutoff = _now() - timedelta(days=30)
    candidates = query.filter(
        TenantCampaignRecipient.destination == normalized_sender,
        TenantCampaignRecipient.sent_at.isnot(None),
        TenantCampaignRecipient.sent_at >= cutoff,
    ).order_by(TenantCampaignRecipient.sent_at.desc(), TenantCampaignRecipient.id.desc()).limit(100).all()
    tenant_ids = {int(row.company_id) for row in candidates}
    if len(tenant_ids) != 1:
        return None
    return candidates[0] if candidates else None


def _client_phone(client):
    raw_phone = str(getattr(client, "whatsapp", None) or getattr(client, "phone", None) or "").strip()
    from services.whatsapp_share_service import normalize_whatsapp_number
    return normalize_whatsapp_number(raw_phone)


def _eligible_clients(company_id, channel="whatsapp"):
    from app import Client
    query = Client.query.filter(Client.company_id == int(company_id), Client.active.is_(True))
    if str(channel or "").strip().lower() != "whatsapp":
        return query.filter(db.false())
    return query.filter(
        func.coalesce(Client.whatsapp, Client.phone).isnot(None),
        func.coalesce(Client.whatsapp, Client.phone) != "",
        Client.whatsapp_marketing_consent == "opted_in",
    ).order_by(Client.id.asc())


def _audience_query(campaign):
    from app import Client, Sale, SaleItem
    data = _json(campaign)
    query = Client.query.filter(Client.company_id == campaign.company_id, Client.active.is_(True))
    audience = str(campaign.audience_segment or "").lower()
    days = max(1, min(int(data.get("days") or 90), 365))
    if audience == "clientes inactivos" or campaign.campaign_type == "recuperacion_clientes_inactivos":
        cutoff = _now() - timedelta(days=days)
        historical = Sale.query.filter(
            Sale.company_id == campaign.company_id,
            Sale.client_id.isnot(None),
            Sale.status.notin_(["cancelada", "anulada"]),
        ).with_entities(Sale.client_id).distinct().subquery()
        recent = Sale.query.filter(
            Sale.company_id == campaign.company_id,
            Sale.client_id.isnot(None),
            Sale.date >= cutoff,
            Sale.status.notin_(["cancelada", "anulada"]),
        ).with_entities(Sale.client_id).distinct().subquery()
        query = query.filter(Client.id.in_(historical), ~Client.id.in_(recent))
    segment = str(data.get("client_segment") or "").strip()
    if segment:
        query = query.filter(Client.notes.ilike(f"%{segment}%"))
    return query.order_by(Client.id.asc())


def prepare_recipients(campaign_id: int, *, company_id: int) -> dict:
    from app import Campaign, Client

    campaign = Campaign.query.filter_by(id=int(campaign_id), company_id=int(company_id)).first()
    if campaign is None:
        raise ValueError("Campaña no encontrada para esta empresa.")
    if campaign.status not in {"APROBADA", "EN_PREPARACION"}:
        raise ValueError("Solo se puede preparar una campaña aprobada.")

    # Tenant CRM is WhatsApp-only. Normalize legacy drafts instead of allowing
    # their previous email/both channel value to re-enable SMTP delivery.
    campaign.channel = "whatsapp"
    campaign_data = _json(campaign)
    campaign_data["channel"] = "whatsapp"
    campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)

    sender_company, connection = _shared_campaign_whatsapp()
    if (
        sender_company is None
        or not connection.get("enabled")
        or not connection.get("phone_number_id")
        or not connection.get("access_token")
    ):
        raise ValueError(
            "El número oficial de Vendedor 24/7 de StockARmobile no está conectado. "
            "Contactá al administrador de StockARmobile para habilitar WhatsApp."
        )
    template_name = str(connection.get("template_name") or "").strip()
    if not template_name:
        raise ValueError(
            "Falta configurar la plantilla aprobada del número Vendedor 24/7 de StockARmobile. "
            "Contactá al administrador para sincronizar una plantilla aprobada."
        )

    # Legacy unsent email rows are retained for audit but permanently skipped.
    for recipient in campaign.recipients:
        if recipient.channel != "whatsapp" and recipient.status in {"pending", "retry_wait"}:
            recipient.status = "skipped"
            recipient.error_reason = "El CRM del comercio opera únicamente por WhatsApp; los envíos por email quedaron deshabilitados."
            recipient.updated_at = _now()

    existing = {
        (r.client_id, r.channel)
        for r in campaign.recipients
        if r.channel == "whatsapp"
    }
    added = 0
    eligible = 0
    clients = _audience_query(campaign).filter(
        Client.whatsapp_marketing_consent == "opted_in"
    ).all()
    for client in clients:
        destination = _client_phone(client)
        if not destination or (client.id, "whatsapp") in existing:
            continue
        eligible += 1
        token = client.marketing_unsubscribe_token or _token()
        client.marketing_unsubscribe_token = token
        db.session.add(
            TenantCampaignRecipient(
                campaign_id=campaign.id,
                company_id=campaign.company_id,
                client_id=client.id,
                channel="whatsapp",
                destination=destination[:255],
                unsubscribe_token=token,
                status="pending",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        existing.add((client.id, "whatsapp"))
        added += 1
    campaign.target_count = len(existing)
    db.session.flush()
    return {"eligible": eligible, "added": added, "target_count": campaign.target_count}


def _render_content(campaign, client):
    replacements = {
        "{{cliente}}": str(client.name or ""),
        "{{nombre}}": str(client.name or ""),
        "{{telefono}}": _client_phone(client),
        "{{producto}}": str(campaign.product.name if campaign.product else ""),
        "{{precio}}": str(campaign.product.price if campaign.product else ""),
    }
    content = str(campaign.content or "")
    for key, value in replacements.items():
        content = content.replace(key, value)
    return content


def _send_whatsapp(recipient, campaign) -> tuple[bool, str, str]:
    from services.ai_agent.whatsapp_service import WhatsAppService

    sender_company, connection = _shared_campaign_whatsapp()
    if (
        sender_company is None
        or not connection.get("enabled")
        or not connection.get("phone_number_id")
        or not connection.get("access_token")
    ):
        return False, "El número oficial de Vendedor 24/7 de StockARmobile no está conectado.", ""

    # Resolve the template from the shared sender's WABA. A tenant-specific
    # template or WhatsApp connection must never override the platform sender.
    template_name = str(connection.get("template_name") or "").strip()
    language = str(connection.get("template_language") or "es_AR").strip()
    if not template_name:
        return False, "No hay plantilla aprobada configurada para el número Vendedor 24/7 de StockARmobile.", ""

    client = recipient.client
    params = [str(client.name or "Cliente"), str(campaign.product.name if campaign.product else "")]
    try:
        result = WhatsAppService.send_template(
            sender_company,
            to=recipient.destination,
            template_name=template_name,
            template_language=language,
            body_parameters=params,
        )
    except Exception as exc:
        return False, str(exc)[:2000], ""
    messages = result.get("messages") if isinstance(result, dict) else None
    provider_id = str(messages[0].get("id") or "") if isinstance(messages, list) and messages and isinstance(messages[0], dict) else ""
    return True, "sent", provider_id


def refresh_attribution(company_id=None):
    from app import Campaign, Client, Sale
    from stockarmobile.models.conversations import Conversation
    query = TenantCampaignRecipient.query.filter_by(status="sent")
    if company_id is not None:
        query = query.filter(TenantCampaignRecipient.company_id == int(company_id))
    recipients = query.all()
    for recipient in recipients:
        tenant_id = recipient.company_id
        client = recipient.client
        phone = _client_phone(client)
        if phone:
            conversation = Conversation.query.filter(
                Conversation.company_id == tenant_id,
                Conversation.external_conversation_id == phone,
                Conversation.created_at >= recipient.sent_at,
                Conversation.created_at <= _now(),
            ).order_by(Conversation.id.asc()).first()
            if conversation is not None:
                recipient.replied_at = recipient.replied_at or conversation.created_at
                if not db.session.query(TenantCampaignAttribution).filter_by(
                    campaign_id=recipient.campaign_id, conversation_id=conversation.id
                ).first():
                    db.session.add(TenantCampaignAttribution(
                        campaign_id=recipient.campaign_id,
                        company_id=tenant_id,
                        client_id=client.id,
                        recipient_id=recipient.id,
                        conversation_id=conversation.id,
                        revenue=0,
                        attribution_type="conversation",
                        created_at=_now(),
                    ))
        if recipient.sent_at:
            sales = Sale.query.filter(
                Sale.company_id == tenant_id,
                Sale.client_id == client.id,
                Sale.date >= recipient.sent_at,
                Sale.date <= recipient.sent_at + timedelta(days=ATTRIBUTION_DAYS),
                Sale.status.notin_(["cancelada", "anulada"]),
            ).all()
            for sale in sales:
                if not db.session.query(TenantCampaignAttribution).filter_by(
                    campaign_id=recipient.campaign_id, sale_id=sale.id
                ).first():
                    db.session.add(TenantCampaignAttribution(
                        campaign_id=recipient.campaign_id,
                        company_id=tenant_id,
                        client_id=client.id,
                        recipient_id=recipient.id,
                        revenue=sale.total_amount or 0,
                        sale_id=sale.id,
                        attribution_type="sale_window",
                        created_at=_now(),
                    ))


def dispatch_due_campaigns(db_session, *, company_id=None, campaign_id=None, limit=10, per_campaign=MAX_RECIPIENTS_PER_CYCLE):
    from flask import current_app
    from app import Campaign
    from sqlalchemy import and_, or_

    if not current_app.config.get("AI_MARKETING_SEND_ENABLED", False):
        return {"campaigns": 0, "sent": 0, "failed": 0, "skipped": 0, "retry_scheduled": 0, "attributed_sales": 0, "disabled": True}

    now = _now()
    query = Campaign.query.filter(
        Campaign.status == "EN_PREPARACION",
        (Campaign.scheduled_at.is_(None) | (Campaign.scheduled_at <= now)),
    )
    if company_id is not None:
        query = query.filter(Campaign.company_id == int(company_id))
    if campaign_id is not None:
        query = query.filter(Campaign.id == int(campaign_id))
    campaigns = query.order_by(Campaign.id.asc()).limit(limit).all()
    summary = {"campaigns": 0, "sent": 0, "failed": 0, "skipped": 0, "retry_scheduled": 0, "attributed_sales": 0}
    for campaign in campaigns:
        campaign.started_at = campaign.started_at or now
        recipients = TenantCampaignRecipient.query.filter(
            TenantCampaignRecipient.campaign_id == campaign.id,
            or_(
                TenantCampaignRecipient.status == "pending",
                and_(
                    TenantCampaignRecipient.status == "retry_wait",
                    TenantCampaignRecipient.updated_at <= now,
                ),
            ),
        ).order_by(TenantCampaignRecipient.id.asc()).limit(per_campaign).with_for_update(skip_locked=True).all()
        campaign.channel = "whatsapp"
        campaign_data = _json(campaign)
        campaign_data["channel"] = "whatsapp"
        campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)
        for recipient in recipients:
            retry_attempt = recipient.status == "retry_wait"
            if recipient.channel != "whatsapp":
                recipient.status = "skipped"
                recipient.error_reason = "El CRM del comercio opera únicamente por WhatsApp; no se realizan envíos por email."
                recipient.updated_at = _now()
                campaign.skipped_count += 1
                summary["skipped"] += 1
                db_session.flush()
                continue
            consent = recipient.client.whatsapp_marketing_consent
            if consent != "opted_in" or not recipient.client.active:
                recipient.status = "skipped"
                recipient.error_reason = "Consentimiento de WhatsApp no vigente o cliente inactivo."
                campaign.skipped_count += 1
                summary["skipped"] += 1
                continue
            ok, detail, provider_id = _send_whatsapp(recipient, campaign)
            if ok:
                recipient.status = "sent"
                recipient.provider_message_id = provider_id or None
                recipient.sent_at = _now()
                campaign.sent_count += 1
                summary["sent"] += 1
            else:
                if not retry_attempt and _is_retryable_delivery_error(detail):
                    recipient.status = "retry_wait"
                    recipient.error_reason = f"Reintento único programado: {detail}"[:2000]
                    recipient.updated_at = _now() + DELIVERY_RETRY_DELAY
                    summary["retry_scheduled"] += 1
                else:
                    recipient.status = "failed"
                    recipient.error_reason = detail
                    recipient.updated_at = _now()
                    campaign.failed_count += 1
                    summary["failed"] += 1
            if ok:
                recipient.updated_at = _now()
            db_session.flush()
        remaining = TenantCampaignRecipient.query.filter(
            TenantCampaignRecipient.campaign_id == campaign.id,
            TenantCampaignRecipient.status.in_(("pending", "retry_wait")),
        ).count()
        if remaining == 0:
            counts = {
                status: TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id, status=status).count()
                for status in ("sent", "failed", "skipped")
            }
            campaign.sent_count = counts["sent"]
            campaign.failed_count = counts["failed"]
            campaign.skipped_count = counts["skipped"]
            if counts["sent"] > 0 and (counts["failed"] > 0 or counts["skipped"] > 0):
                campaign.status = "ENVIADA_PARCIAL"
            elif counts["sent"] > 0:
                campaign.status = "ENVIADA"
            elif counts["failed"] > 0:
                campaign.status = "FALLIDA"
            else:
                campaign.status = "SIN_ENVIO"
            campaign.finished_at = _now()
        summary["campaigns"] += 1
        refresh_attribution(company_id=campaign.company_id)
    refresh_attribution(company_id=company_id)
    db_session.commit()
    return summary


def start_campaign_dispatch_worker(flask_app, *, poll_interval: int = 5) -> bool:
    """Start a lightweight in-process queue worker; no external cron is required."""
    if flask_app.testing or "pytest" in sys.modules:
        return False

    global _AUTO_DISPATCH_WORKER_STARTED
    with _AUTO_DISPATCH_WORKER_LOCK:
        if _AUTO_DISPATCH_WORKER_STARTED:
            return False
        _AUTO_DISPATCH_WORKER_STARTED = True

    def _run():
        while True:
            try:
                if not flask_app.config.get("AI_MARKETING_SEND_ENABLED", False):
                    time.sleep(poll_interval)
                    continue

                with flask_app.app_context():
                    from app import Campaign, db
                    from sqlalchemy import or_

                    now = _now()
                    due_campaigns = (
                        Campaign.query.filter(
                            Campaign.status == "EN_PREPARACION",
                            or_(Campaign.scheduled_at.is_(None), Campaign.scheduled_at <= now),
                        )
                        .order_by(Campaign.id.asc())
                        .all()
                    )
                    for campaign in due_campaigns:
                        result = dispatch_due_campaigns(
                            db.session,
                            company_id=campaign.company_id,
                            campaign_id=campaign.id,
                            limit=1,
                            per_campaign=MAX_RECIPIENTS_PER_CYCLE,
                        )
                        if result.get("sent") or result.get("failed") or result.get("skipped"):
                            flask_app.logger.info(
                                "Marketing IA auto-dispatch campaign=%s company=%s sent=%s failed=%s skipped=%s",
                                campaign.id,
                                campaign.company_id,
                                result.get("sent", 0),
                                result.get("failed", 0),
                                result.get("skipped", 0),
                            )
                        db.session.expire_all()
                    db.session.remove()
            except Exception:
                flask_app.logger.exception("Marketing IA in-process dispatch worker cycle failed")
                try:
                    with flask_app.app_context():
                        from app import db
                        db.session.rollback()
                        db.session.remove()
                except Exception:
                    flask_app.logger.exception("Marketing IA worker could not clean up its database session")
            time.sleep(poll_interval)

    thread = threading.Thread(
        target=_run,
        name="stockarmobile-marketing-dispatch",
        daemon=True,
    )
    thread.start()
    flask_app.logger.info("Marketing IA in-process dispatch worker started (poll=%ss)", poll_interval)
    return True


def campaign_metrics(company_id: int, campaign_id: int) -> dict:
    from app import Campaign
    campaign = Campaign.query.filter_by(id=campaign_id, company_id=company_id).first()
    if campaign is None:
        raise ValueError("Campaña no encontrada.")
    recipients = TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id)
    attributions = TenantCampaignAttribution.query.filter_by(campaign_id=campaign.id)
    revenue = db.session.query(func.coalesce(func.sum(TenantCampaignAttribution.revenue), 0)).filter(
        TenantCampaignAttribution.campaign_id == campaign.id,
        TenantCampaignAttribution.attribution_type == "sale_window",
    ).scalar() or 0
    return {
        "total": recipients.count(),
        "pending": recipients.filter(TenantCampaignRecipient.status.in_(("pending", "retry_wait"))).count(),
        "sent": recipients.filter_by(status="sent").count(),
        "failed": recipients.filter_by(status="failed").count(),
        "skipped": recipients.filter_by(status="skipped").count(),
        "replied": recipients.filter(TenantCampaignRecipient.replied_at.isnot(None)).count(),
        "attributed_conversations": attributions.filter_by(attribution_type="conversation").count(),
        "attributed_sales": attributions.filter_by(attribution_type="sale_window").count(),
        "attributed_revenue": float(revenue),
    }


class TenantCampaignRecipient(db.Model):
    __tablename__ = "ai_campaign_recipients"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("ai_campaigns.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    destination = db.Column(db.String(255), nullable=False)
    unsubscribe_token = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    provider_message_id = db.Column(db.String(255))
    error_reason = db.Column(db.String(2000))
    sent_at = db.Column(db.DateTime)
    replied_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)
    client = db.relationship("Client")
    campaign = db.relationship("Campaign", backref="recipients")


class TenantCampaignAttribution(db.Model):
    __tablename__ = "ai_campaign_attributions"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("ai_campaigns.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("ai_campaign_recipients.id"))
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"))
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"))
    revenue = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    attribution_type = db.Column(db.String(30), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
