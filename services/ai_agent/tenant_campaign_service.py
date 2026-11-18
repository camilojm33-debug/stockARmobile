"""Tenant-scoped Marketing IA delivery engine.

Tenant CRM campaigns always use the WhatsApp connection registered by their own company and branch.
Campaigns, recipients, consent, conversations, replies and attribution remain tenant scoped.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import requests
from datetime import datetime, timedelta

from sqlalchemy import Index, UniqueConstraint, func

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive

CHANNELS = {"whatsapp"}
SENDABLE_STATUSES = {"EN_PREPARACION"}
MAX_RECIPIENTS_PER_CYCLE = 50
DELIVERY_RETRY_DELAY = timedelta(minutes=15)
ATTRIBUTION_DAYS = 30
MAX_CAMPAIGN_COPY_LENGTH = 900
CAMPAIGN_OPT_OUT_TEXT = "Para dejar de recibir mensajes, respondé BAJA."
_AUTO_DISPATCH_WORKER_LOCK = threading.Lock()
_AUTO_DISPATCH_WORKER_STARTED = False


def _now():
    return utcnow_naive()


def _is_retryable_delivery_error(detail: str) -> bool:
    message = str(detail or "").lower()
    status_match = re.search(r"http\s+(\d{3})", message)
    if status_match:
        return int(status_match.group(1)) == 429
    # WhatsApp network timeouts are ambiguous: the provider may have accepted
    # the message before the connection failed, so do not retry uncertain sends.
    return False


def _is_uncertain_delivery_error(detail: str) -> bool:
    return "resultado incierto" in str(detail or "").casefold()


def _token():
    return secrets.token_urlsafe(48)


def _json(campaign):
    try:
        value = json.loads(campaign.system_data_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _campaign_branch_id(campaign):
    data = _json(campaign)
    try:
        value = int(data.get("branch_id"))
    except (TypeError, ValueError):
        value = None
    return value if value and value > 0 else None


def resolve_tenant_campaign_sender(company, branch_id=None, *, require_ready=True):
    """Resolve the WhatsApp sender owned by one tenant company and branch.

    The SuperAdmin commercial company is deliberately never consulted here.
    A Phone Number ID must resolve to this exact company and branch.
    """
    from app import Branch
    from stockarmobile.branch_context import get_default_branch_id
    from services.ai_agent.branch_vendor_service import branch_vendor_access
    from services.ai_agent.config_service import (
        company_branch_for_whatsapp_phone_id,
        get_whatsapp_connection,
    )

    if company is None or not getattr(company, "id", None):
        raise ValueError("No se pudo identificar el comercio propietario de la campaña.")
    from services.ai_agent.config_service import is_commercial_company
    if is_commercial_company(company):
        raise ValueError(
            "El CRM comercial de SuperAdmin utiliza su propio motor de campañas y no puede enviar desde el CRM de un comercio."
        )

    try:
        selected_branch_id = int(branch_id) if branch_id is not None else None
    except (TypeError, ValueError):
        selected_branch_id = None
    if not selected_branch_id:
        selected_branch_id = get_default_branch_id(int(company.id))
    branch = (
        Branch.query.filter_by(
            id=int(selected_branch_id),
            company_id=int(company.id),
            active=True,
        ).first()
        if selected_branch_id else None
    )
    if branch is None:
        raise ValueError("La campaña no tiene una sucursal activa válida. Seleccioná una sucursal e intentá nuevamente.")

    access = branch_vendor_access(company, branch)
    if not access.get("allowed"):
        raise ValueError(
            access.get("reason")
            or "Esta sucursal necesita Vendedor IA activo para utilizar su número en campañas."
        )

    connection = get_whatsapp_connection(company, branch_id=branch.id)
    if not require_ready:
        return branch, connection

    phone_number_id = str(connection.get("phone_number_id") or "").strip()
    waba_id = str(connection.get("business_account_id") or "").strip()
    access_token = str(connection.get("access_token") or "").strip()
    if not connection.get("enabled") or not phone_number_id or not access_token or not waba_id:
        raise ValueError(
            "El comercio todavía no tiene un WhatsApp propio conectado y habilitado en esta sucursal. "
            "Conectalo desde Vendedor IA mediante Meta Embedded Signup."
        )
    if str(connection.get("token_health") or "").strip().lower() == "expired":
        raise ValueError("El token de Meta de este comercio venció. Reconectá WhatsApp desde Vendedor IA antes de enviar.")

    registered_company, registered_branch = company_branch_for_whatsapp_phone_id(phone_number_id)
    if (
        registered_company is None
        or registered_branch is None
        or int(registered_company.id) != int(company.id)
        or int(registered_branch.id) != int(branch.id)
    ):
        raise ValueError(
            "El número de WhatsApp no está registrado a nombre de este comercio y sucursal. "
            "Volvé a conectar el número desde Meta Embedded Signup."
        )
    return branch, connection


def _tenant_campaign_sender_binding(company, branch, connection):
    """Persist non-secret sender identity to detect connection changes before delivery."""
    return {
        "company_id": int(company.id),
        "branch_id": int(branch.id),
        "phone_number_id": str(connection.get("phone_number_id") or "").strip(),
        "business_account_id": str(connection.get("business_account_id") or "").strip(),
    }

def resolve_tenant_campaign_reply(*, phone_number_id: str, sender: str, context_message_id: str = ""):
    """Resolve a reply arriving on a merchant's registered WhatsApp number.

    The inbound Phone Number ID identifies one merchant/branch. We then require
    the original campaign recipient to belong to that same merchant; message-ID
    context is preferred, with a recent same-tenant destination fallback.
    """
    from services.ai_agent.config_service import company_branch_for_whatsapp_phone_id
    from services.saas_commercial_whatsapp import get_commercial_company
    from services.whatsapp_share_service import normalize_whatsapp_number

    company, branch = company_branch_for_whatsapp_phone_id(phone_number_id)
    if company is None or branch is None:
        return None
    platform_company = get_commercial_company()
    if platform_company is not None and int(platform_company.id) == int(company.id):
        return None

    normalized_sender = normalize_whatsapp_number(sender)
    if not normalized_sender:
        return None

    def _same_branch(recipient) -> bool:
        campaign = recipient.campaign
        saved_branch_id = _campaign_branch_id(campaign) if campaign is not None else None
        if saved_branch_id is not None:
            return int(saved_branch_id) == int(branch.id)
        # Older campaign drafts predate branch_id in system_data_json. They
        # belong to the default branch only, never to another branch's sender.
        return bool(branch.is_default)

    query = TenantCampaignRecipient.query.filter(
        TenantCampaignRecipient.company_id == int(company.id),
        TenantCampaignRecipient.channel == "whatsapp",
        TenantCampaignRecipient.status == "sent",
        TenantCampaignRecipient.destination == normalized_sender,
    )
    referenced_id = str(context_message_id or "").strip()
    if referenced_id:
        matches = query.filter(
            TenantCampaignRecipient.provider_message_id == referenced_id
        ).limit(2).all()
        return matches[0] if len(matches) == 1 and _same_branch(matches[0]) else None

    cutoff = _now() - timedelta(days=30)
    candidates = query.filter(
        TenantCampaignRecipient.sent_at.isnot(None),
        TenantCampaignRecipient.sent_at >= cutoff,
    ).order_by(
        TenantCampaignRecipient.sent_at.desc(),
        TenantCampaignRecipient.id.desc(),
    ).limit(100).all()
    return next((row for row in candidates if _same_branch(row)), None)


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
    )
    tenant_ids = [
        int(row[0])
        for row in candidates.with_entities(TenantCampaignRecipient.company_id).distinct().limit(2).all()
    ]
    if len(tenant_ids) != 1:
        return None
    return candidates.filter(
        TenantCampaignRecipient.company_id == tenant_ids[0]
    ).order_by(TenantCampaignRecipient.sent_at.desc(), TenantCampaignRecipient.id.desc()).first()


def _client_phone(client):
    raw_phone = str(getattr(client, "whatsapp", None) or "").strip()
    from services.whatsapp_share_service import normalize_whatsapp_number
    normalized = normalize_whatsapp_number(raw_phone)
    return normalized if 8 <= len(normalized) <= 15 else ""


def _eligible_clients(company_id, channel="whatsapp"):
    from app import Client
    query = Client.query.filter(Client.company_id == int(company_id), Client.active.is_(True))
    if str(channel or "").strip().lower() != "whatsapp":
        return query.filter(Client.id == -1)
    return query.filter(
        Client.whatsapp.isnot(None),
        Client.whatsapp != "",
        Client.whatsapp_marketing_consent == "opted_in",
        Client.whatsapp_marketing_consent_recorded_at.isnot(None),
        func.length(func.trim(Client.whatsapp_marketing_consent_source)) > 0,
        func.length(func.trim(Client.whatsapp_marketing_consent_evidence)) > 0,
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
    from app import Campaign, Client, Company

    campaign = Campaign.query.filter_by(
        id=int(campaign_id), company_id=int(company_id)
    ).with_for_update().first()
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

    company = Company.query.filter_by(id=int(campaign.company_id), active=True).first()
    if company is None:
        raise ValueError("El comercio propietario de la campaña no existe o está inactivo.")
    branch, connection = resolve_tenant_campaign_sender(
        company,
        _campaign_branch_id(campaign),
        require_ready=True,
    )
    # Once queued, the campaign sender is immutable. A running campaign may
    # never switch numbers halfway through delivery, even if preparation is
    # requested again after a merchant reconnects Meta.
    current_sender_binding = _tenant_campaign_sender_binding(company, branch, connection)
    saved_sender_binding = campaign_data.get("whatsapp_sender")
    if campaign.status == "EN_PREPARACION":
        if not isinstance(saved_sender_binding, dict) or any(
            str(saved_sender_binding.get(field) or "") != str(current_sender_binding.get(field) or "")
            for field in ("company_id", "branch_id", "phone_number_id", "business_account_id")
        ):
            raise ValueError(
                "La campaña ya está en cola con otro remitente o con una conexión anterior. "
                "No puede cambiar de número durante el envío. Cancelala y creá una nueva campaña "
                "después de validar el WhatsApp del comercio."
            )

    # Store the resolved branch even for older approved drafts without metadata.
    # The identity is re-checked at send time to prevent silent sender changes.
    campaign_data["branch_id"] = int(branch.id)
    stored_template = campaign_data.get("whatsapp_template")
    if isinstance(stored_template, dict) and stored_template.get("name"):
        # Campaign templates are selected per draft from this branch's WABA.
        # They are intentionally independent from the Vendedor IA default template.
        template = _fetch_campaign_template(
            connection,
            selected_name=str(stored_template.get("name") or ""),
            selected_language=str(stored_template.get("language") or "es_AR"),
            allow_single_auto_select=False,
        )
    else:
        # Backward compatibility for older drafts that predate the campaign picker.
        template = _fetch_campaign_template(connection)
    if not _render_content(
        campaign,
        type("PreviewClient", (), {"name": "Cliente", "whatsapp": ""})(),
        include_opt_out=not _template_includes_opt_out(template),
    ).strip():
        raise ValueError("El mensaje de campaña no puede quedar vacío.")
    campaign_data["whatsapp_template"] = template
    campaign_data["whatsapp_sender"] = current_sender_binding
    campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)

    # Legacy unsent email rows are retained for audit but permanently skipped.
    campaign_recipients = TenantCampaignRecipient.query.filter_by(
        campaign_id=campaign.id
    ).order_by(TenantCampaignRecipient.id.asc()).all()
    for recipient in campaign_recipients:
        if recipient.channel != "whatsapp" and recipient.status in {"pending", "retry_wait"}:
            recipient.status = "skipped"
            recipient.error_reason = "El CRM del comercio opera únicamente por WhatsApp; los envíos por email quedaron deshabilitados."
            recipient.updated_at = _now()

    existing = {
        (r.client_id, r.channel)
        for r in TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        if r.channel == "whatsapp"
    }
    added = 0
    eligible = 0
    clients = _audience_query(campaign).filter(
        Client.whatsapp_marketing_consent == "opted_in",
        Client.whatsapp_marketing_consent_recorded_at.isnot(None),
        func.length(func.trim(Client.whatsapp_marketing_consent_source)) > 0,
        func.length(func.trim(Client.whatsapp_marketing_consent_evidence)) > 0,
    ).all()
    for client in clients:
        destination = _client_phone(client)
        if not destination or (client.id, "whatsapp") in existing:
            continue
        resolved_parameters = _tenant_campaign_parameter_resolution(
            campaign,
            client,
            company,
            template,
            campaign_data,
        )
        if not resolved_parameters.get("valid"):
            missing = ", ".join(resolved_parameters.get("missing_fields") or [])
            errors = " ".join(resolved_parameters.get("errors") or [])
            raise ValueError(
                "No se pudo preparar la campaña: faltan parámetros para "
                f"{client.name or 'un cliente'} ({missing or errors}). "
                "Completá la ficha o agregá valores predeterminados antes de volver a aprobar."
            )
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


def _template_includes_opt_out(template: dict | None) -> bool:
    """Whether Meta's static template body already contains an unsubscribe instruction."""
    body = str((template or {}).get("body_text") or "")
    return bool(
        re.search(r"\bBAJA\b", body, re.IGNORECASE)
        and re.search(r"(dejar de recibir|no recibir)", body, re.IGNORECASE)
    )


def _render_content(campaign, client, *, include_opt_out: bool = True):
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
    content = content.strip()
    if not content:
        raise ValueError("El mensaje de campaña está vacío.")
    if include_opt_out and CAMPAIGN_OPT_OUT_TEXT.lower() not in content.lower():
        content = f"{content}\n\n{CAMPAIGN_OPT_OUT_TEXT}"
    if len(content) > MAX_CAMPAIGN_COPY_LENGTH:
        raise ValueError(
            f"El mensaje personalizado supera el máximo de {MAX_CAMPAIGN_COPY_LENGTH} caracteres."
        )
    if re.search(r"\{\{[^{}]+\}\}", content):
        raise ValueError("El mensaje contiene variables sin resolver; corregí el texto antes de aprobar.")
    return content


def _tenant_campaign_parameter_resolution(campaign, client, company, template, campaign_data=None):
    """Resolve this campaign's positional parameters from one tenant client record."""
    from services.campaign_template_engine import (
        SCOPE_MERCHANT_CLIENTS,
        campaign_settings,
        parse_defaults,
        resolve_parameters,
    )

    data = campaign_data if isinstance(campaign_data, dict) else _json(campaign)
    settings = campaign_settings(SCOPE_MERCHANT_CLIENTS, data)
    goal_key = settings.get("campaign_goal")
    goal_defaults = settings.get("parameter_defaults") or {}
    configured_defaults = parse_defaults(
        SCOPE_MERCHANT_CLIENTS,
        data.get("parameter_defaults"),
    )
    defaults = dict(goal_defaults)
    defaults.update(configured_defaults)

    template_body = str((template or {}).get("body_text") or "")
    positions = sorted({
        int(value) for value in re.findall(r"\{\{\s*(\d+)\s*\}\}", template_body)
    })
    expected_count = len(positions)
    if positions != list(range(1, expected_count + 1)):
        expected_count = -1

    content = _render_content(
        campaign,
        client,
        include_opt_out=not _template_includes_opt_out(template),
    )
    product = getattr(campaign, "product", None)
    context = {
        "contacto": str(getattr(client, "name", "") or "").strip(),
        "empresa": str(getattr(company, "name", "") or "").strip(),
        "comercio": str(getattr(company, "name", "") or "").strip(),
        "telefono": _client_phone(client),
        "email": str(getattr(client, "email", "") or "").strip(),
        "localidad": str(getattr(client, "city", "") or "").strip(),
        "provincia": str(getattr(client, "province", "") or "").strip(),
        "rubro": str(getattr(client, "customer_type", "") or "").strip(),
        "producto": str(getattr(product, "name", "") or "").strip(),
        "precio": str(getattr(product, "price", "") or "").strip(),
        "mensaje": content,
        "asunto": str(getattr(campaign, "objective", "") or getattr(campaign, "title", "") or "").strip(),
    }
    mapping = data.get("whatsapp_parameter_fields") or settings.get("whatsapp_parameter_fields")
    resolved = resolve_parameters(
        SCOPE_MERCHANT_CLIENTS,
        mapping,
        context,
        defaults,
        expected_count=expected_count,
        goal_key=goal_key,
    )
    resolved["goal_key"] = goal_key
    resolved["template_body"] = template_body
    return resolved


def _fetch_campaign_template(
    connection: dict,
    *,
    selected_name: str | None = None,
    selected_language: str | None = None,
    allow_single_auto_select: bool = True,
) -> dict:
    """Resolve the approved marketing template from the tenant branch WABA."""
    from services.ai_agent.whatsapp_campaign_template_service import get_campaign_template

    return get_campaign_template(
        connection,
        selected_name=selected_name,
        selected_language=selected_language,
        allow_single_auto_select=allow_single_auto_select,
    )


def _send_whatsapp(recipient, campaign) -> tuple[bool, str, str]:
    from app import Company
    from services.ai_agent.whatsapp_service import WhatsAppService

    company = Company.query.filter_by(id=int(campaign.company_id), active=True).first()
    if company is None:
        return False, "El comercio propietario de la campaña no existe o está inactivo.", ""

    client = recipient.client
    if (
        int(getattr(recipient, "company_id", 0) or 0) != int(company.id)
        or int(getattr(recipient, "campaign_id", 0) or 0) != int(campaign.id)
        or client is None
        or int(getattr(client, "company_id", 0) or 0) != int(company.id)
    ):
        return False, "El destinatario no pertenece a la campaña y al comercio que solicita el envío.", ""

    try:
        branch, connection = resolve_tenant_campaign_sender(
            company,
            _campaign_branch_id(campaign),
            require_ready=True,
        )
    except ValueError as exc:
        return False, str(exc)[:2000], ""

    campaign_data = _json(campaign)
    saved_sender = campaign_data.get("whatsapp_sender")
    current_sender = _tenant_campaign_sender_binding(company, branch, connection)
    if not isinstance(saved_sender, dict):
        return (
            False,
            "La campaña fue preparada con una configuración anterior y no tiene remitente propio validado. "
            "Volvé a preparar y aprobar la campaña desde el CRM del comercio.",
            "",
        )
    for field in ("company_id", "branch_id", "phone_number_id", "business_account_id"):
        if str(saved_sender.get(field) or "") != str(current_sender.get(field) or ""):
            return (
                False,
                "La conexión de WhatsApp del comercio cambió desde que se preparó la campaña. "
                "Volvé a validar la plantilla y aprobar la campaña antes de enviarla.",
                "",
            )

    template = campaign_data.get("whatsapp_template") or {}
    template_name = str(template.get("name") or "").strip()
    language = str(template.get("language") or "").strip()
    if (
        not template_name
        or not language
        or not template.get("body_text")
        or str(template.get("status") or "APPROVED").upper() != "APPROVED"
        or template.get("campaign_compatible") is False
    ):
        return False, "La plantilla aprobada cambió desde que se preparó la campaña; volvé a validar y aprobar.", ""

    try:
        resolved = _tenant_campaign_parameter_resolution(
            campaign,
            client,
            company,
            template,
            campaign_data,
        )
        if not resolved.get("valid"):
            missing = ", ".join(resolved.get("missing_fields") or [])
            errors = " ".join(resolved.get("errors") or [])
            return False, f"Faltan parámetros de plantilla: {missing or errors}.", ""
        params = resolved["values"]
    except ValueError as exc:
        return False, str(exc), ""
    try:
        result = WhatsAppService.send_template(
            company,
            to=recipient.destination,
            template_name=template_name,
            template_language=language,
            body_parameters=params,
            branch_id=branch.id,
            connection=connection,
        )
    except requests.Timeout as exc:
        return False, f"Resultado incierto: Meta no respondió a tiempo ({exc}).", ""
    except requests.ConnectionError as exc:
        return False, f"Resultado incierto: se perdió la conexión con Meta ({exc}).", ""
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
        return {"campaigns": 0, "sent": 0, "failed": 0, "skipped": 0, "uncertain": 0, "retry_scheduled": 0, "attributed_sales": 0, "disabled": True}

    stale_query = TenantCampaignRecipient.query.filter(
        TenantCampaignRecipient.status == "sending",
        TenantCampaignRecipient.sending_at <= _now() - timedelta(minutes=10),
    )
    if company_id is not None:
        stale_query = stale_query.filter(TenantCampaignRecipient.company_id == int(company_id))
    if campaign_id is not None:
        stale_query = stale_query.filter(TenantCampaignRecipient.campaign_id == int(campaign_id))
    stale_campaign_ids = set()
    for stale in stale_query.all():
        stale.status = "uncertain"
        stale.error_reason = (
            "El proceso terminó durante el envío y no se puede confirmar si Meta aceptó el mensaje. "
            "No se reintentó para evitar duplicados."
        )
        stale.updated_at = _now()
        stale_campaign_ids.add(stale.campaign_id)

    cancelled_query = Campaign.query.filter(
        Campaign.status == "CANCELADA",
        Campaign.finished_at.is_(None),
    )
    if company_id is not None:
        cancelled_query = cancelled_query.filter(Campaign.company_id == int(company_id))
    if campaign_id is not None:
        cancelled_query = cancelled_query.filter(Campaign.id == int(campaign_id))
    for cancelled in cancelled_query.limit(100).all():
        in_flight = TenantCampaignRecipient.query.filter_by(
            campaign_id=cancelled.id, status="sending"
        ).count()
        if not in_flight:
            cancelled.finished_at = _now()
            counts = {
                status: TenantCampaignRecipient.query.filter_by(
                    campaign_id=cancelled.id, status=status
                ).count()
                for status in ("sent", "failed", "skipped")
            }
            cancelled.sent_count = counts["sent"]
            cancelled.failed_count = counts["failed"]
            cancelled.skipped_count = counts["skipped"]
    if stale_campaign_ids or cancelled_query.count():
        db_session.commit()

    query = Campaign.query.filter(
        Campaign.status == "EN_PREPARACION",
        (Campaign.scheduled_at.is_(None) | (Campaign.scheduled_at <= _now())),
    )
    if company_id is not None:
        query = query.filter(Campaign.company_id == int(company_id))
    if campaign_id is not None:
        query = query.filter(Campaign.id == int(campaign_id))
    campaign_ids = [
        row[0]
        for row in query.with_entities(Campaign.id).order_by(Campaign.id.asc()).limit(limit).all()
    ]
    summary = {
        "campaigns": 0, "sent": 0, "failed": 0, "skipped": 0,
        "uncertain": 0, "retry_scheduled": 0, "attributed_sales": 0,
    }
    for current_campaign_id in campaign_ids:
        processed = 0
        while processed < max(1, int(per_campaign)):
            campaign = Campaign.query.filter_by(id=current_campaign_id).with_for_update().first()
            if campaign is None or campaign.status != "EN_PREPARACION":
                break
            campaign.channel = "whatsapp"

            recipient = TenantCampaignRecipient.query.filter(
                TenantCampaignRecipient.campaign_id == campaign.id,
                or_(
                    TenantCampaignRecipient.status == "pending",
                    and_(
                        TenantCampaignRecipient.status == "retry_wait",
                        TenantCampaignRecipient.updated_at <= _now(),
                    ),
                ),
            ).order_by(TenantCampaignRecipient.id.asc()).with_for_update(skip_locked=True).first()
            if recipient is None:
                db_session.commit()
                break

            retry_attempt = recipient.status == "retry_wait"
            recipient_id = recipient.id
            recipient.status = "sending"
            recipient.sending_at = _now()
            recipient.updated_at = _now()
            campaign.started_at = campaign.started_at or _now()
            db_session.commit()
            processed += 1
            db_session.expire_all()

            campaign = Campaign.query.filter_by(id=current_campaign_id).first()
            recipient = TenantCampaignRecipient.query.get(recipient_id)
            if campaign is None or recipient is None:
                continue
            if campaign.status != "EN_PREPARACION":
                recipient.status = "skipped"
                recipient.error_reason = "La campaña se canceló antes de iniciar este envío."
                recipient.updated_at = _now()
                db_session.commit()
                continue

            client = recipient.client
            consent_is_valid = bool(
                client
                and client.whatsapp_marketing_consent == "opted_in"
                and client.whatsapp_marketing_consent_recorded_at
                and client.whatsapp_marketing_consent_source
                and client.whatsapp_marketing_consent_evidence
                and client.active
                and _client_phone(client) == recipient.destination
            )
            if recipient.channel != "whatsapp":
                recipient.status = "skipped"
                recipient.error_reason = (
                    "El CRM del comercio opera únicamente por WhatsApp; no se realizan envíos por email."
                )
                recipient.updated_at = _now()
                db_session.commit()
                summary["skipped"] += 1
                continue
            if not consent_is_valid:
                recipient.status = "skipped"
                recipient.error_reason = (
                    "WhatsApp, consentimiento documentado o estado activo cambió después de preparar la audiencia."
                )
                recipient.updated_at = _now()
                db_session.commit()
                summary["skipped"] += 1
                continue

            ok, detail, provider_id = _send_whatsapp(recipient, campaign)
            recipient.updated_at = _now()
            if ok:
                recipient.status = "sent"
                recipient.provider_message_id = provider_id or None
                recipient.provider_status = "sent"
                recipient.sent_at = _now()
                summary["sent"] += 1
            elif _is_uncertain_delivery_error(detail):
                recipient.status = "uncertain"
                recipient.error_reason = detail[:2000]
                summary["uncertain"] += 1
            elif not retry_attempt and _is_retryable_delivery_error(detail):
                recipient.status = "retry_wait"
                recipient.error_reason = f"Reintento único programado: {detail}"[:2000]
                recipient.updated_at = _now() + DELIVERY_RETRY_DELAY
                summary["retry_scheduled"] += 1
            else:
                recipient.status = "failed"
                recipient.error_reason = detail[:2000]
                summary["failed"] += 1
            db_session.commit()

        campaign = Campaign.query.filter_by(id=current_campaign_id).first()
        if campaign is not None:
            active_count = TenantCampaignRecipient.query.filter(
                TenantCampaignRecipient.campaign_id == current_campaign_id,
                TenantCampaignRecipient.status.in_(("pending", "retry_wait", "sending")),
            ).count()
            if active_count == 0:
                counts = {
                    status: TenantCampaignRecipient.query.filter_by(
                        campaign_id=current_campaign_id, status=status
                    ).count()
                    for status in ("sent", "failed", "skipped", "uncertain")
                }
                campaign.sent_count = counts["sent"]
                campaign.failed_count = counts["failed"]
                campaign.skipped_count = counts["skipped"]
                if campaign.status == "EN_PREPARACION":
                    if counts["sent"] and (counts["failed"] or counts["skipped"] or counts["uncertain"]):
                        campaign.status = "ENVIADA_PARCIAL"
                    elif counts["sent"]:
                        campaign.status = "ENVIADA"
                    elif counts["uncertain"]:
                        campaign.status = "ENVIADA_PARCIAL"
                    elif counts["failed"]:
                        campaign.status = "FALLIDA"
                    else:
                        campaign.status = "SIN_ENVIO"
                    campaign.finished_at = _now()
                elif campaign.status == "CANCELADA":
                    campaign.finished_at = campaign.finished_at or _now()
                db_session.commit()
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
                    from app import db

                    result = dispatch_due_campaigns(
                        db.session,
                        limit=100,
                        per_campaign=MAX_RECIPIENTS_PER_CYCLE,
                    )
                    if any(
                        result.get(key)
                        for key in ("campaigns", "sent", "failed", "skipped", "uncertain")
                    ):
                        flask_app.logger.info(
                            "Marketing IA auto-dispatch campaigns=%s sent=%s failed=%s skipped=%s uncertain=%s",
                            result.get("campaigns", 0),
                            result.get("sent", 0),
                            result.get("failed", 0),
                            result.get("skipped", 0),
                            result.get("uncertain", 0),
                        )
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
        "pending": recipients.filter(TenantCampaignRecipient.status.in_(("pending", "retry_wait", "sending"))).count(),
        "sent": recipients.filter_by(status="sent").count(),
        "failed": recipients.filter_by(status="failed").count(),
        "skipped": recipients.filter_by(status="skipped").count(),
        "uncertain": recipients.filter_by(status="uncertain").count(),
        "delivered": recipients.filter(TenantCampaignRecipient.delivered_at.isnot(None)).count(),
        "read": recipients.filter(TenantCampaignRecipient.read_at.isnot(None)).count(),
        "replied": recipients.filter(TenantCampaignRecipient.replied_at.isnot(None)).count(),
        "attributed_conversations": attributions.filter_by(attribution_type="conversation").count(),
        "attributed_sales": attributions.filter_by(attribution_type="sale_window").count(),
        "attributed_revenue": float(revenue),
    }


class TenantCampaignRecipient(db.Model):
    __tablename__ = "ai_campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "client_id", "channel", name="uq_ai_campaign_recipient_target"),
        Index("ix_ai_campaign_recipients_provider_message_id", "provider_message_id"),
    )
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("ai_campaigns.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    destination = db.Column(db.String(255), nullable=False)
    unsubscribe_token = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    provider_message_id = db.Column(db.String(255))
    provider_status = db.Column(db.String(30))
    error_reason = db.Column(db.String(2000))
    sent_at = db.Column(db.DateTime)
    sending_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    read_at = db.Column(db.DateTime)
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
