"""Tenant-scoped Marketing IA delivery engine.

This module is deliberately separate from the SuperAdmin acquisition CRM.
It reuses the existing SMTP configuration and WhatsApp Cloud transport while
keeping every query, recipient and attribution strictly tenant scoped.
"""
from __future__ import annotations

import json
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote

from sqlalchemy import func

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive

CHANNELS = {"email", "whatsapp", "both"}
SENDABLE_STATUSES = {"EN_PREPARACION"}
MAX_RECIPIENTS_PER_CYCLE = 50
DELIVERY_RETRY_DELAY = timedelta(minutes=15)
ATTRIBUTION_DAYS = 30


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


def _client_phone(client):
    return str(getattr(client, "whatsapp", None) or getattr(client, "phone", None) or "").strip()


def _eligible_clients(company_id, channel):
    from app import Client
    query = Client.query.filter(Client.company_id == int(company_id), Client.active.is_(True))
    if channel == "email":
        query = query.filter(Client.email.isnot(None), Client.email != "", Client.email_marketing_consent == "opted_in")
    else:
        query = query.filter(
            func.coalesce(Client.whatsapp, Client.phone).isnot(None),
            func.coalesce(Client.whatsapp, Client.phone) != "",
            Client.whatsapp_marketing_consent == "opted_in",
        )
    return query.order_by(Client.id.asc())


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
    if campaign.channel not in CHANNELS:
        raise ValueError("Canal de campaña inválido.")

    channels = ("email", "whatsapp") if campaign.channel == "both" else (campaign.channel,)

    if campaign.channel in {"whatsapp", "both"}:
        from services.ai_agent.config_service import get_whatsapp_connection
        from app import Company

        company = Company.query.filter_by(id=campaign.company_id, active=True).first()
        connection = get_whatsapp_connection(company)
        if (
            not connection.get("enabled")
            or not connection.get("phone_number_id")
            or not connection.get("access_token")
        ):
            raise ValueError(
                "WhatsApp no está conectado para esta empresa. "
                "Configurá el número de WhatsApp en Agentes IA o elegí Email como canal."
            )
        template_name = str(
            _json(campaign).get("whatsapp_template_name")
            or connection.get("template_name")
            or ""
        ).strip()
        if not template_name:
            raise ValueError(
                "WhatsApp está conectado, pero falta una plantilla aprobada. "
                "Configurá la plantilla o elegí Email como canal."
            )

    existing = {(r.client_id, r.channel) for r in campaign.recipients}
    added = 0
    eligible = 0
    for channel in channels:
        clients = _audience_query(campaign).filter(
            Client.email_marketing_consent == "opted_in" if channel == "email"
            else Client.whatsapp_marketing_consent == "opted_in"
        ).all()
        for client in clients:
            destination = str(client.email or "").strip() if channel == "email" else _client_phone(client)
            if not destination or (client.id, channel) in existing:
                continue
            eligible += 1
            token = client.marketing_unsubscribe_token or _token()
            client.marketing_unsubscribe_token = token
            db.session.add(
                TenantCampaignRecipient(
                    campaign_id=campaign.id,
                    company_id=campaign.company_id,
                    client_id=client.id,
                    channel=channel,
                    destination=destination[:255],
                    unsubscribe_token=token,
                    status="pending",
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            existing.add((client.id, channel))
            added += 1
    campaign.target_count = len(existing)
    # audience_count represents the business segment detected by the AI;
    # target_count represents channel-eligible recipients. Never overwrite
    # the analytical audience with channel consent filtering.
    db.session.flush()
    return {"eligible": eligible, "added": added, "target_count": campaign.target_count}


def _render_content(campaign, client):
    replacements = {
        "{{cliente}}": str(client.name or ""),
        "{{nombre}}": str(client.name or ""),
        "{{email}}": str(client.email or ""),
        "{{telefono}}": _client_phone(client),
        "{{producto}}": str(campaign.product.name if campaign.product else ""),
        "{{precio}}": str(campaign.product.price if campaign.product else ""),
    }
    content = str(campaign.content or "")
    for key, value in replacements.items():
        content = content.replace(key, value)
    return content


def _send_email(recipient, campaign) -> tuple[bool, str, str]:
    from flask import current_app
    host = str(current_app.config.get("SMTP_HOST") or "").strip()
    user = str(current_app.config.get("SMTP_USER") or "").strip()
    password = current_app.config.get("SMTP_PASSWORD") or ""
    if not host or not user:
        return False, "SMTP no configurado.", ""
    client = recipient.client
    content = _render_content(campaign, client)
    unsub = str(current_app.config.get("APP_URL") or "").rstrip("/") + "/agentes-ia/campanas/unsubscribe/" + quote(recipient.unsubscribe_token)
    msg = EmailMessage()
    msg["Subject"] = str(campaign.title or "Novedad")[:255]
    msg["From"] = str(current_app.config.get("SMTP_FROM_EMAIL") or user)
    msg["To"] = recipient.destination
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(content)
    try:
        with smtplib.SMTP(host, int(current_app.config.get("SMTP_PORT") or 587), timeout=30) as server:
            if bool(current_app.config.get("SMTP_USE_TLS", True)):
                server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True, "sent", ""
    except Exception as exc:
        return False, str(exc)[:2000], ""


def _send_whatsapp(recipient, campaign) -> tuple[bool, str, str]:
    from flask import current_app
    from services.ai_agent.whatsapp_service import WhatsAppService
    from services.ai_agent.config_service import get_whatsapp_connection
    from app import Company

    company = Company.query.filter_by(id=campaign.company_id, active=True).first()
    if company is None:
        return False, "Empresa no disponible.", ""
    connection = get_whatsapp_connection(company)
    if not connection.get("enabled") or not connection.get("phone_number_id") or not connection.get("access_token"):
        return False, "WhatsApp no está conectado para esta empresa.", ""
    template_name = str(_json(campaign).get("whatsapp_template_name") or connection.get("template_name") or "").strip()
    language = str(_json(campaign).get("whatsapp_template_language") or connection.get("template_language") or "es_AR").strip()
    if not template_name:
        return False, "No hay plantilla WhatsApp aprobada configurada.", ""
    client = recipient.client
    params = [str(client.name or "Cliente"), str(campaign.product.name if campaign.product else "")]
    try:
        result = WhatsAppService.send_template(
            company,
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


def dispatch_due_campaigns(db_session, *, company_id=None, limit=10, per_campaign=MAX_RECIPIENTS_PER_CYCLE):
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
        for recipient in recipients:
            retry_attempt = recipient.status == "retry_wait"
            consent = (
                recipient.client.email_marketing_consent if recipient.channel == "email"
                else recipient.client.whatsapp_marketing_consent
            )
            if consent != "opted_in" or not recipient.client.active:
                recipient.status = "skipped"
                recipient.error_reason = "Consentimiento no vigente o cliente inactivo."
                campaign.skipped_count += 1
                summary["skipped"] += 1
                continue
            if recipient.channel == "email":
                ok, detail, provider_id = _send_email(recipient, campaign)
            else:
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
