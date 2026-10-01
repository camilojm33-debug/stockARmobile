"""Isolated WhatsApp acquisition channel for SuperAdmin commercial leads."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import or_

from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent, AgentConfiguration, Conversation, ConversationMessage


COMMERCIAL_COMPANY_MARKER = "whatsapp_commercial"
COMMERCIAL_AGENT_NAME = "Comercial IA"
COMMERCIAL_CHANNEL = "whatsapp_commercial"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _company_preferences(company) -> dict:
    raw = getattr(company, "preferences_json", None) or ""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def get_commercial_company():
    """Resolve the dedicated internal company without trusting tenant routing."""
    from app import Company

    explicit_id = str(os.getenv("WHATSAPP_COMMERCIAL_COMPANY_ID") or "").strip()
    candidates = []
    if explicit_id.isdigit():
        company = Company.query.filter_by(id=int(explicit_id), active=True).first()
        if company is not None and _company_preferences(company).get("internal_channel") == COMMERCIAL_COMPANY_MARKER:
            return company

    for company in (
        Company.query
        .filter(Company.active.is_(True), Company.preferences_json.contains(COMMERCIAL_COMPANY_MARKER))
        .order_by(Company.id.asc())
        .all()
    ):
        prefs = _company_preferences(company)
        if str(prefs.get("internal_channel") or "").strip().lower() == COMMERCIAL_COMPANY_MARKER:
            candidates.append(company)
    return candidates[0] if candidates else None


def commercial_phone_number_id() -> str:
    """Return the SuperAdmin commercial phone ID from env or the internal company."""
    env_enabled = str(os.getenv("WHATSAPP_COMMERCIAL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    env_phone = str(os.getenv("WHATSAPP_COMMERCIAL_PHONE_NUMBER_ID") or "").strip()
    if env_enabled and env_phone:
        return env_phone
    try:
        company = get_commercial_company()
        if company is not None:
            from services.ai_agent.config_service import get_ai_preferences
            stored = get_ai_preferences(company)["whatsapp"]
            return str(stored.get("phone_number_id") or "").strip()
    except Exception:
        return ""
    return ""


def is_commercial_phone_number_id(phone_number_id: str) -> bool:
    configured = commercial_phone_number_id()
    return bool(configured and configured == str(phone_number_id or "").strip())


def _default_model() -> str:
    provider = (os.getenv("AI_PROVIDER") or "openai_compatible").strip().lower()
    if provider == "gemini":
        return (os.getenv("GEMINI_MODEL") or "gemini-3.6-flash").strip()
    if provider == "openai":
        return (os.getenv("OPENAI_MODEL") or "gpt-4.1-mini").strip()
    return (os.getenv("AI_PROVIDER_MODEL") or "gpt-4.1-mini").strip()


def ensure_commercial_agent(company_id: int) -> Agent:
    agent = (
        Agent.query
        .filter(Agent.company_id == company_id, Agent.name == COMMERCIAL_AGENT_NAME)
        .order_by(Agent.id.asc())
        .first()
    )
    if agent is None:
        agent = Agent(
            company_id=company_id,
            name=COMMERCIAL_AGENT_NAME,
            description="Agente IA para captación y atención comercial de StockArMobile.",
            active=True,
        )
        db.session.add(agent)
        db.session.flush()

    config = (
        AgentConfiguration.query
        .filter(
            AgentConfiguration.company_id == company_id,
            AgentConfiguration.agent_id == agent.id,
        )
        .order_by(AgentConfiguration.id.asc())
        .first()
    )
    if config is None:
        config = AgentConfiguration(
            company_id=company_id,
            agent_id=agent.id,
            model=_default_model(),
            language="es-AR",
            max_tokens=900,
            temperature=0.20,
            system_prompt=(
                "Atendé prospectos de StockArMobile. No uses ni consultes datos de comercios clientes. "
                "No generes pedidos ni cobros. Explicá el producto y guiá al prospecto hacia la contratación. "
                "Antes de informar precios o planes, consultá la herramienta de oferta comercial."
            ),
        )
        db.session.add(config)
        db.session.flush()
    return agent


def _superadmin_actor_id():
    from app import User

    actor = (
        User.query
        .filter(User.role == "superadmin", User.active.is_(True))
        .order_by(User.id.asc())
        .first()
    )
    return actor.id if actor else None


def capture_inbound_lead(sender: str, text: str):
    """Create/update a SaaS CRM lead, never bind it to a tenant company."""
    from app import SaaSLead, SaaSLeadConsent

    normalized = "".join(ch for ch in str(sender or "") if ch.isdigit())[:40]
    if not normalized:
        return None

    lead = (
        SaaSLead.query
        .filter(or_(SaaSLead.whatsapp == normalized, SaaSLead.phone == normalized))
        .order_by(SaaSLead.id.desc())
        .first()
    )
    now = _utcnow()
    message_note = f"[WhatsApp comercial {now.isoformat(timespec='seconds')}] {str(text or '').strip()[:1000]}"

    if lead is None:
        actor_id = _superadmin_actor_id()
        if actor_id is None:
            raise RuntimeError("No existe un usuario SuperAdmin activo para registrar el lead.")
        lead = SaaSLead(
            company_name="Prospecto WhatsApp",
            contact_name="Prospecto WhatsApp",
            whatsapp=normalized,
            source="whatsapp_comercial",
            status="nuevo",
            priority="alta",
            notes=message_note,
            whatsapp_consent_status="unknown",
            email_consent_status="unknown",
            phone_consent_status="unknown",
            do_not_contact=False,
            created_by_user_id=actor_id,
            company_id=None,
            captured_at=now,
            validated_at=now,
        )
        db.session.add(lead)
        db.session.flush()
    else:
        previous = (lead.notes or "").strip()
        lead.notes = f"{previous}\n{message_note}".strip()[-6000:]
        lead.updated_at = now

    if lead.consent is None:
        consent = SaaSLeadConsent(
            lead_id=lead.id,
            whatsapp_status="unknown",
            email_status="unknown",
            phone_status="unknown",
            unsubscribe_token=os.urandom(36).hex(),
            created_at=now,
            updated_at=now,
        )
        db.session.add(consent)
    db.session.commit()
    return lead.id



def _normalized_lead_email(email: str) -> str:
    return str(email or "").strip().lower()


def _make_activation_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(36)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_commercial_checkout(*, sender: str, plan_code: str, payer_email: str, company_name: str, back_url: str, notification_url: str) -> dict:
    """Create/reuse a paid SaaS checkout for the current WhatsApp prospect."""
    from app import Plan, SaaSCommercialCheckout, SaaSLead
    from services.mercadopago_service import MercadoPagoService

    company = get_commercial_company()
    if company is None:
        raise RuntimeError("El canal Comercial IA no está inicializado.")

    normalized_plan = str(plan_code or "").strip().lower()
    normalized_email = _normalized_lead_email(payer_email)
    normalized_name = str(company_name or "").strip()
    if not normalized_name or len(normalized_name) < 2:
        raise ValueError("Necesito el nombre de la empresa.")
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Necesito un email válido para iniciar el pago.")

    plan = Plan.query.filter_by(code=normalized_plan, active=True).first()
    if plan is None or str(plan.code).strip().lower() == "trial":
        raise ValueError("Seleccioná un plan pago vigente.")
    amount = float(plan.price or 0)
    if amount <= 0:
        raise ValueError("El plan seleccionado no tiene un importe válido.")

    normalized_phone = "".join(ch for ch in str(sender or "") if ch.isdigit())[:40]
    lead = (
        SaaSLead.query
        .filter(SaaSLead.whatsapp == normalized_phone)
        .order_by(SaaSLead.id.desc())
        .first()
    )
    if lead is None:
        lead_id = capture_inbound_lead(sender, "")
        lead = SaaSLead.query.filter_by(id=lead_id).first() if lead_id else None
    if lead is None:
        raise RuntimeError("No pude asociar el checkout con el prospecto de WhatsApp.")

    pending = (
        SaaSCommercialCheckout.query
        .filter(
            SaaSCommercialCheckout.lead_id == lead.id,
            SaaSCommercialCheckout.plan_code == normalized_plan,
            SaaSCommercialCheckout.status == "pending",
        )
        .order_by(SaaSCommercialCheckout.id.desc())
        .first()
    )
    if pending is not None and pending.checkout_url and pending.preapproval_id:
        return {
            "success": True,
            "status": "pending",
            "checkout_url": pending.checkout_url,
            "preapproval_id": pending.preapproval_id,
            "amount": amount,
            "currency": plan.currency or "ARS",
            "plan_name": plan.name,
            "checkout_id": pending.id,
        }

    token_raw, token_hash = _make_activation_token()
    from services.ai_agent.config_service import encrypt_secret, decrypt_secret
    external_reference = (
        f"stockarmobile|flow:commercial_checkout|lead_id:{lead.id}|"
        f"plan_code:{normalized_plan}|checkout_id:pending|nonce:{uuid.uuid4().hex}"
    )

    response = MercadoPagoService().create_preapproval(
        reason=f"StockArMobile - Plan {plan.name}",
        payer_email=normalized_email,
        external_reference=external_reference,
        amount=amount,
        currency=plan.currency or "ARS",
        frequency=1,
        frequency_type="months",
        notification_url=notification_url,
        back_url=back_url,
    )
    preapproval_id = str(response.get("id") or "").strip()
    checkout_url = str(response.get("init_point") or "").strip()
    if not preapproval_id or not checkout_url:
        raise RuntimeError("Mercado Pago no devolvió un enlace de pago válido.")

    external_reference = external_reference.replace("checkout_id:pending", f"checkout_id:{preapproval_id}")
    checkout = SaaSCommercialCheckout(
        lead_id=lead.id,
        plan_id=plan.id,
        plan_code=normalized_plan,
        company_name=normalized_name[:160],
        payer_email=normalized_email[:160],
        phone=normalized_phone,
        preapproval_id=preapproval_id,
        external_reference=external_reference,
        checkout_url=checkout_url,
        status="pending",
        activation_token_hash=token_hash,
        activation_token_encrypted=encrypt_secret(token_raw),
    )
    db.session.add(checkout)
    lead.company_name = normalized_name[:160]
    lead.email = normalized_email[:160]
    lead.phone = normalized_phone
    lead.status = "propuesta"
    db.session.commit()
    return {
        "success": True,
        "status": "pending",
        "checkout_url": checkout_url,
        "preapproval_id": preapproval_id,
        "amount": amount,
        "currency": plan.currency or "ARS",
        "plan_name": plan.name,
        "checkout_id": checkout.id,
    }


def activate_commercial_checkout(*, preapproval: dict) -> dict | None:
    """Create the tenant account after Mercado Pago authorizes the commercial preapproval."""
    from app import (
        Company,
        SaaSCommercialCheckout,
        Subscription,
        User,
    )
    from services.referral_service import ReferralService
    from services.saas_ops_service import SaaSOpsService
    from services.subscription_service import SubscriptionService

    preapproval_id = str((preapproval or {}).get("id") or "").strip()
    if not preapproval_id:
        return None
    checkout = (
        SaaSCommercialCheckout.query
        .filter_by(preapproval_id=preapproval_id)
        .order_by(SaaSCommercialCheckout.id.asc())
        .first()
    )
    if checkout is None:
        return None

    status = str((preapproval or {}).get("status") or "").strip().lower()
    if status not in {"authorized", "approved"}:
        if status in {"cancelled", "canceled", "expired", "paused"}:
            checkout.status = status
            db.session.commit()
        return {"status": status or "pending", "checkout_id": checkout.id}

    if checkout.status == "activated" and checkout.company_id:
        company = Company.query.get(checkout.company_id)
        activation_url = None
        token_cipher = str(checkout.activation_token_encrypted or "").strip()
        if token_cipher:
            from services.ai_agent.config_service import decrypt_secret
            raw_token = decrypt_secret(token_cipher)
            if raw_token:
                from flask import url_for
                activation_url = url_for("auth.activate_commercial", token=raw_token, _external=True)
        return {
            "status": "activated",
            "checkout_id": checkout.id,
            "company_id": company.id if company else None,
            "activation_url": activation_url,
        }

    existing_company = (
        Company.query
        .filter(db.func.lower(Company.name) == checkout.company_name.lower())
        .order_by(Company.id.asc())
        .first()
    )
    if existing_company is not None and existing_company.id != 18:
        raise RuntimeError("Ya existe una empresa con ese nombre.")

    now = _utcnow()
    company = existing_company
    if company is None:
        company = Company(
            name=checkout.company_name[:160],
            active=True,
            created_at=now,
        )
        db.session.add(company)
        db.session.flush()

    existing_user = User.query.filter(db.func.lower(User.email) == checkout.payer_email.lower()).first()
    if existing_user is not None and existing_user.company_id not in {None, company.id}:
        raise RuntimeError("El email de contratación ya pertenece a otra empresa.")

    user = existing_user
    if user is None:
        username_base = "".join(ch.lower() if ch.isalnum() else "-" for ch in checkout.payer_email.split("@")[0]).strip("-") or "cliente"
        username = username_base
        suffix = 2
        while User.query.filter_by(username=username).first() is not None:
            username = f"{username_base}-{suffix}"
            suffix += 1
        user = User(username=username, email=checkout.payer_email, company_id=company.id, role="admin", active=True, auth_provider="local")
        user.set_password(secrets.token_urlsafe(20))
        db.session.add(user)
        db.session.flush()
    else:
        user.company_id = company.id
        user.role = "admin"
        user.active = True

    plan = db.session.get(__import__("app").Plan, checkout.plan_id)
    if plan is None:
        raise RuntimeError("El plan contratado ya no está disponible.")

    subscription = (
        Subscription.query
        .filter_by(company_id=company.id, status="active")
        .order_by(Subscription.id.asc())
        .first()
    )
    if subscription is None:
        result = SubscriptionService.run_command(
            db.session,
            SubscriptionService.CreateSubscriptionCommand(
                company_id=company.id,
                actor_user_id=None,
                actor_role="system",
                origin="commercial_checkout",
                idempotency_key=f"commercial-activate:{checkout.id}",
                plan_id=plan.id,
                status="active",
                start_date=now,
                next_billing_date=now + timedelta(days=int(plan.duration_days or 30)),
                renewal_enabled=True,
                metadata={
                    "commercial_checkout_id": checkout.id,
                    "mercadopago_preapproval_id": preapproval_id,
                    "payer_email": checkout.payer_email,
                },
            ),
        )
        subscription = db.session.get(Subscription, result.subscription_id)
    else:
        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.start_date = now
        subscription.starts_at = now
        subscription.next_billing_date = now + timedelta(days=int(plan.duration_days or 30))
        subscription.ends_at = subscription.next_billing_date
        subscription.renewal_enabled = True
        subscription.auto_renew = True

    subscription.mercadopago_subscription_id = preapproval_id
    subscription.external_reference = checkout.external_reference
    metadata = {}
    try:
        metadata = json.loads(subscription.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    metadata.update({
        "commercial_checkout_id": checkout.id,
        "mercadopago_preapproval_id": preapproval_id,
        "mercadopago_status": status,
        "payer_email": checkout.payer_email,
    })
    subscription.metadata_json = json.dumps(metadata, ensure_ascii=False)

    checkout.status = "activated"
    checkout.company_id = company.id
    checkout.user_id = user.id
    checkout.activated_at = now

    activation_url = None
    token_cipher = str(checkout.activation_token_encrypted or "").strip()
    if token_cipher:
        from services.ai_agent.config_service import decrypt_secret
        raw_token = decrypt_secret(token_cipher)
        if raw_token:
            from flask import url_for
            activation_url = url_for("auth.activate_commercial", token=raw_token, _external=True)

    lead = checkout.lead
    lead.company_id = company.id
    lead.assigned_user_id = user.id
    lead.status = "ganado"
    lead.converted_at = now
    lead.updated_at = now

    SaaSOpsService.register_signup(db.session, company=company, user=user)
    db.session.commit()
    return {
        "status": "activated",
        "checkout_id": checkout.id,
        "company_id": company.id,
        "user_id": user.id,
        "payer_email": checkout.payer_email,
        "activation_url": activation_url,
    }


def activation_token_for_checkout(checkout) -> str | None:
    if not checkout or checkout.status != "activated":
        return None
    # The raw token is intentionally never persisted; activation link must be
    # delivered before this request reaches activation or regenerated by support.
    return None


def get_commercial_conversation(company_id: int, sender: str) -> Conversation:
    agent = ensure_commercial_agent(company_id)
    conversation = (
        Conversation.query
        .filter(
            Conversation.company_id == company_id,
            Conversation.agent_id == agent.id,
            Conversation.channel == COMMERCIAL_CHANNEL,
            Conversation.external_conversation_id == sender,
        )
        .order_by(Conversation.id.asc())
        .first()
    )
    if conversation is not None:
        return conversation

    conversation = Conversation(
        company_id=company_id,
        agent_id=agent.id,
        channel=COMMERCIAL_CHANNEL,
        external_conversation_id=sender,
        metadata_json={
            "whatsapp_user": sender,
            "commercial_acquisition": True,
        },
    )
    db.session.add(conversation)
    db.session.flush()
    return conversation


def process_commercial_message(*, phone_number_id: str, sender: str, external_id: str, text: str) -> dict:
    """Process one inbound commercial message with strict internal-company isolation."""
    from services.ai_agent.config_service import get_whatsapp_connection
    from services.ai_agent.orchestrator_v2 import AgentRuntime
    from services.ai_agent.usage_service import can_use_ai
    from services.ai_agent.whatsapp_service import WhatsAppService

    company = get_commercial_company()
    if company is None:
        raise RuntimeError("No existe la empresa interna de WhatsApp comercial.")

    connection = get_whatsapp_connection(company)
    if (
        not connection.get("enabled")
        or connection.get("phone_number_id") != str(phone_number_id or "").strip()
    ):
        raise RuntimeError("WhatsApp comercial todavía no está habilitado/configurado.")

    lead_id = capture_inbound_lead(sender, text)
    conversation = get_commercial_conversation(company.id, sender)

    access = can_use_ai(company, "comercial")
    if not access.allowed:
        raise RuntimeError(access.reason or "El Comercial IA no está disponible.")

    result = AgentRuntime.process(
        company_id=company.id,
        conversation_id=conversation.id,
        message=text,
        channel=COMMERCIAL_CHANNEL,
        sender_id=None,
        external_message_id=external_id,
        idempotency_key=f"whatsapp-commercial:{external_id}",
        metadata={
            "phone_number_id": phone_number_id,
            "from": sender,
            "channel": COMMERCIAL_CHANNEL,
            "commercial_acquisition": True,
        },
    )

    content = str(result.get("content") or "").strip()
    if content:
        WhatsAppService.send_text(company, to=sender, body=content)
        lead = (
            __import__("app").SaaSLead.query
            .filter_by(id=lead_id)
            .first()
            if lead_id
            else None
        )
        if lead is not None:
            now = _utcnow()
            lead.last_contacted_at = now
            lead.last_contact_channel = "whatsapp"
            lead.contact_count = int(lead.contact_count or 0) + 1
            db.session.commit()

    return {
        "status": result.get("status") or "completed",
        "content": content,
        "company_id": company.id,
        "conversation_id": conversation.id,
        "lead_id": lead_id,
    }
