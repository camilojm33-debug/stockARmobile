"""Commercial acquisition CRM services for SuperAdmin."""
from __future__ import annotations

import csv
import json
import re
import secrets
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from html import escape
from io import BytesIO, StringIO
from urllib.parse import quote

from openpyxl import load_workbook

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
MAX_IMPORT_ROWS = 10000
VALID_CHANNELS = {"email", "whatsapp", "both"}
CONSENT_VALUES = {"opted_in", "opted_out", "unknown"}


def _eligible_consent_values(channel: str) -> tuple[str, ...]:
    if channel == "email":
        return ("opted_in",)
    if channel == "whatsapp":
        return ("opted_in",)
    raise ValueError("Canal de campaña no soportado.")


def _consent_is_eligible(channel: str, consent_status: str | None) -> bool:
    return str(consent_status or "unknown").strip().lower() in _eligible_consent_values(channel)


def _apply_consent_eligibility(query, lead_model, channel: str):
    field_name = "email_consent_status" if channel == "email" else "whatsapp_consent_status"
    return query.filter(getattr(lead_model, field_name).in_(_eligible_consent_values(channel)))

HEADER_ALIASES = {
    "company_name": {"comercio", "empresa", "nombre comercio", "nombre empresa", "business", "company"},
    "contact_name": {"contacto", "contacto comercial", "nombre", "persona de contacto"},
    "email": {"email", "e-mail", "correo", "correo electronico", "mail"},
    "phone": {"telefono", "teléfono", "phone", "tel"},
    "whatsapp": {"whatsapp", "whatsapp comercial"},
    "industry": {"rubro", "industria", "categoria", "categoría", "sector"},
    "subindustry": {"subrubro", "subcategoria", "subcategoría"},
    "province": {"provincia", "state"},
    "locality": {"localidad", "ciudad", "municipio", "city"},
    "address": {"direccion", "dirección", "domicilio"},
    "website": {"web", "sitio web", "website", "url"},
    "instagram": {"instagram", "ig"},
    "facebook": {"facebook"},
    "source": {"fuente", "source", "origen"},
    "source_url": {"url fuente", "fuente url", "source url"},
    "segment": {"segmento", "segment"},
    "email_consent_status": {"email consent", "consentimiento email", "permiso email", "email permitido"},
    "whatsapp_consent_status": {"whatsapp consent", "consentimiento whatsapp", "permiso whatsapp", "whatsapp permitido"},
    "phone_consent_status": {"phone consent", "consentimiento llamada", "permiso llamada", "llamada permitida"},
    "do_not_contact": {"no contactar", "do not contact", "bloqueado"},
}

def _clean(value) -> str:
    return str(value or "").strip()

def _normalize_header(value) -> str:
    return re.sub(r"\s+", " ", _clean(value).lower().replace("_", " "))

def normalize_email(value: str | None) -> str | None:
    email = _clean(value).lower()
    if not email:
        return None
    return email if EMAIL_RE.match(email) else None


def _campaign_email_is_valid(lead, destination: str | None = None) -> bool:
    email = normalize_email(getattr(lead, "email", None))
    if email is None or getattr(lead, "email_status", "unknown") == "invalid":
        return False
    return destination is None or email == normalize_email(destination)

def normalize_phone(value: str | None) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    return digits or None

def parse_consent(value, *, default: str = "unknown") -> str:
    raw = _clean(value).lower()
    if not raw:
        return default if default in CONSENT_VALUES else "unknown"
    if raw in CONSENT_VALUES:
        return raw
    if raw in {"si", "sí", "yes", "true", "1", "permitido", "acepto", "aceptado"}:
        return "opted_in"
    if raw in {"no", "false", "0", "bloqueado", "rechazado", "baja"}:
        return "opted_out"
    return "unknown"

def parse_bool(value) -> bool:
    return _clean(value).lower() in {"1", "true", "yes", "si", "sí", "x", "bloqueado"}

def lead_score(row: dict) -> int:
    score = 0
    if row.get("industry"): score += 15
    if row.get("email"): score += 10
    if row.get("phone") or row.get("whatsapp"): score += 10
    if row.get("website"): score += 10
    if row.get("instagram") or row.get("facebook"): score += 5
    industry = _clean(row.get("industry")).lower()
    if any(term in industry for term in (
        "ferreter", "corral", "indument", "mayorista", "distrib", "pet shop",
        "mascota", "comput", "diet", "repuesto", "electro", "muebler", "bazar",
        "zapater", "perfumer", "supermerc", "comercio",
    )):
        score += 25
    if row.get("province"): score += 5
    if row.get("locality"): score += 5
    if row.get("website") and any(x in _clean(row.get("website")).lower() for x in ("shop", "tienda", "store", "ecommerce", "mercadoshops")):
        score += 15
    return min(score, 100)

def _header_map(headers):
    normalized = {_normalize_header(h): h for h in headers if _clean(h)}
    result = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key = _normalize_header(alias)
            if key in normalized:
                result[field] = normalized[key]
                break

    # Heuristic fallback for contact columns with extra qualifiers.
    for normalized_header, original in normalized.items():
        if "email" in normalized_header and "consent" not in normalized_header and "permiso" not in normalized_header:
            result.setdefault("email", original)
        if "whatsapp" in normalized_header and "consent" not in normalized_header and "permiso" not in normalized_header:
            result.setdefault("whatsapp", original)
        if any(token in normalized_header for token in ("telefono", "tel", "celular", "movil", "mobile")) and "consent" not in normalized_header and "permiso" not in normalized_header:
            result.setdefault("phone", original)

    return result


def _build_row(raw: dict, header_map: dict) -> dict:
    def value(field):
        source = header_map.get(field)
        return _clean(raw.get(source)) if source else ""
    email_raw = value("email")
    email = normalize_email(email_raw)
    row = {
        "company_name": value("company_name"),
        "contact_name": value("contact_name") or "Contacto comercial",
        "email": email,
        "phone": normalize_phone(value("phone")),
        "whatsapp": normalize_phone(value("whatsapp")),
        "industry": value("industry"),
        "subindustry": value("subindustry"),
        "province": value("province"),
        "locality": value("locality"),
        "address": value("address"),
        "website": value("website"),
        "instagram": value("instagram"),
        "facebook": value("facebook"),
        "source": value("source") or "import_publico",
        "source_url": value("source_url"),
        "segment": value("segment") or value("industry"),
        "email_consent_status": parse_consent(value("email_consent_status")),
        "whatsapp_consent_status": parse_consent(value("whatsapp_consent_status")),
        "phone_consent_status": parse_consent(value("phone_consent_status")),
        "do_not_contact": parse_bool(value("do_not_contact")),
        "email_status": "valid" if email else ("invalid" if email_raw else "unknown"),
        "phone_status": "valid" if value("phone") else "unknown",
    }
    row["lead_score"] = lead_score(row)
    row["valid"] = bool(row["company_name"] and (row["email"] or row["phone"] or row["whatsapp"] or row["website"]))
    return row

def parse_prospect_file(file_bytes: bytes, filename: str) -> dict:
    name = _clean(filename).lower()
    if name.endswith(".csv"):
        raw_text = file_bytes.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw_text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(StringIO(raw_text), dialect=dialect)
        headers = reader.fieldnames or []
        header_map = _header_map(headers)
        rows, invalid = [], 0
        for raw in reader:
            if len(rows) + invalid >= MAX_IMPORT_ROWS:
                break
            row = _build_row(raw, header_map)
            if row["valid"]:
                rows.append(row)
            else:
                invalid += 1
        return {"format": "csv", "rows": rows, "invalid_count": invalid, "headers": headers}
    if name.endswith((".xlsx", ".xlsm")):
        workbook = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
        values = workbook.active.iter_rows(values_only=True)
        headers = [str(v or "") for v in next(values, [])]
        header_map = _header_map(headers)
        rows, invalid = [], 0
        for values_row in values:
            if len(rows) + invalid >= MAX_IMPORT_ROWS:
                break
            row = _build_row(dict(zip(headers, values_row)), header_map)
            if row["valid"]:
                rows.append(row)
            else:
                invalid += 1
        return {"format": "xlsx", "rows": rows, "invalid_count": invalid, "headers": headers}
    raise ValueError("Formato no soportado. Usá CSV o XLSX.")

def _merge_if_empty(target, field: str, value) -> bool:
    if value and not str(getattr(target, field, "") or "").strip():
        setattr(target, field, value)
        return True
    return False

def import_prospect_rows(db_session, *, rows: list[dict], filename: str, user_id: int, source: str = "import_publico"):
    from app import SaaSLead, SaaSLeadConsent, SaaSLeadImport, utcnow
    imp = SaaSLeadImport(
        file_name=_clean(filename)[:255] or "import.csv",
        file_format="xlsx" if filename.lower().endswith((".xlsx", ".xlsm")) else "csv",
        source=_clean(source).lower()[:80] or "import_publico",
        status="completed", rows_read=len(rows), created_by_user_id=user_id,
    )
    db_session.add(imp)
    db_session.flush()
    seen = set()
    for row in rows:
        email = row.get("email")
        phones = {p for p in (row.get("phone"), row.get("whatsapp")) if p}
        key = ("email", email) if email else (("phone", sorted(phones)[0]) if phones else None)
        if key and key in seen:
            imp.duplicate_count += 1
            continue
        if key: seen.add(key)
        lead = SaaSLead.query.filter(SaaSLead.email == email).first() if email else None
        if lead is None and phones:
            lead = SaaSLead.query.filter(
                (SaaSLead.phone.in_(list(phones))) | (SaaSLead.whatsapp.in_(list(phones)))
            ).first()
        now = utcnow()
        if lead is None:
            lead = SaaSLead(
                company_name=row["company_name"][:160],
                contact_name=row["contact_name"][:160],
                email=email, phone=row.get("phone") or None, whatsapp=row.get("whatsapp") or None,
                industry=row.get("industry")[:100] if row.get("industry") else None,
                subindustry=row.get("subindustry")[:100] if row.get("subindustry") else None,
                province=row.get("province")[:100] if row.get("province") else None,
                locality=row.get("locality")[:120] if row.get("locality") else None,
                address=row.get("address")[:255] if row.get("address") else None,
                website=row.get("website")[:255] if row.get("website") else None,
                instagram=row.get("instagram")[:255] if row.get("instagram") else None,
                facebook=row.get("facebook")[:255] if row.get("facebook") else None,
                source=(row.get("source") or source)[:80],
                source_url=row.get("source_url")[:500] if row.get("source_url") else None,
                segment=row.get("segment")[:100] if row.get("segment") else None,
                lead_score=row.get("lead_score", 0),
                email_status=row.get("email_status", "unknown"),
                phone_status=row.get("phone_status", "unknown"),
                email_consent_status=row.get("email_consent_status", "unknown"),
                whatsapp_consent_status=row.get("whatsapp_consent_status", "unknown"),
                phone_consent_status=row.get("phone_consent_status", "unknown"),
                do_not_contact=bool(row.get("do_not_contact")),
                captured_at=now, validated_at=now, created_by_user_id=user_id,
            )
            db_session.add(lead)
            db_session.flush()
            imp.inserted_count += 1
        else:
            changed = False
            for field in ("contact_name","email","phone","whatsapp","industry","subindustry","province","locality","address","website","instagram","facebook","source","source_url","segment"):
                changed |= _merge_if_empty(lead, field, row.get(field))
            if row.get("lead_score", 0) > (lead.lead_score or 0):
                lead.lead_score = row["lead_score"]; changed = True
            for field in ("email_consent_status","whatsapp_consent_status","phone_consent_status"):
                incoming = row.get(field, "unknown")
                if incoming != "unknown" and getattr(lead, field, "unknown") == "unknown":
                    setattr(lead, field, incoming); changed = True
            if row.get("do_not_contact"):
                if not lead.do_not_contact: changed = True
                lead.do_not_contact = True
                lead.do_not_contact_at = lead.do_not_contact_at or now
            lead.validated_at = now
            if changed:
                lead.updated_at = now
                imp.updated_count += 1
            else:
                imp.duplicate_count += 1
        consent = lead.consent or SaaSLeadConsent(lead_id=lead.id)
        if not consent.unsubscribe_token:
            consent.unsubscribe_token = secrets.token_urlsafe(48)
        if lead.email_consent_status != "unknown":
            consent.email_status = lead.email_consent_status
        if lead.whatsapp_consent_status != "unknown":
            consent.whatsapp_status = lead.whatsapp_consent_status
        if lead.phone_consent_status != "unknown":
            consent.phone_status = lead.phone_consent_status
        db_session.add(consent)
    imp.completed_at = utcnow()
    db_session.flush()
    return imp

def segment_filters_from_request(request) -> dict:
    try:
        score = max(0, min(100, int(request.form.get("min_score") or request.args.get("min_score") or 0)))
    except (TypeError, ValueError):
        score = 0

    def clean_filter(value):
        cleaned = _clean(value)
        return "" if cleaned.lower() in {"todos", "todas", "all", "*"} else cleaned

    return {
        "industry": clean_filter(request.form.get("industry") or request.args.get("industry")),
        "province": clean_filter(request.form.get("province") or request.args.get("province")),
        "locality": clean_filter(request.form.get("locality") or request.args.get("locality")),
        "segment": clean_filter(request.form.get("segment") or request.args.get("segment")),
        "min_score": score,
    }


def register_lead_suppression(db_session, *, lead, user_id: int, reason: str = "superadmin_delete") -> bool:
    """Persist identity suppression before deleting a CRM lead."""
    from app import SaaSLeadSuppression

    email = str(lead.email or "").strip().lower() or None
    phone_raw = lead.whatsapp or lead.phone or ""
    phone = "".join(ch for ch in str(phone_raw) if ch.isdigit()) or None
    company_name = " ".join(str(lead.company_name or "").strip().lower().split()) or None

    existing = None
    if email:
        existing = SaaSLeadSuppression.query.filter_by(email=email).first()
    if existing is None and phone:
        existing = SaaSLeadSuppression.query.filter_by(phone=phone).first()
    if existing is None and not email and not phone and company_name:
        existing = SaaSLeadSuppression.query.filter_by(company_name=company_name).first()

    if existing is None:
        existing = SaaSLeadSuppression(
            email=email,
            phone=phone,
            company_name=company_name,
            reason=(reason or "superadmin_delete").strip()[:500],
            source="superadmin_delete",
            created_by_user_id=user_id,
        )
        db_session.add(existing)
    else:
        existing.reason = (reason or existing.reason or "superadmin_delete").strip()[:500]
        existing.created_by_user_id = user_id
    db_session.flush()
    return True


def delete_saas_lead(db_session, lead_id: int, *, suppressing_user_id: int | None = None) -> dict:
    """Permanently remove one SuperAdmin CRM lead and its CRM-only children.

    Commercial checkout records are retained as billing history; their lead
    reference is detached so deleting a CRM prospect never breaks activation or
    Mercado Pago reconciliation.
    """
    from app import SaaSAlert, SaaSCampaignEvent, SaaSCampaignRecipient, SaaSCommercialCheckout, SaaSLead, SaaSLeadConsent, SaaSTask

    lead = db_session.get(SaaSLead, int(lead_id))
    if lead is None:
        raise ValueError("Prospecto no encontrado.")

    lead_id = int(lead.id)
    register_lead_suppression(
        db_session,
        lead=lead,
        user_id=int(suppressing_user_id or lead.created_by_user_id),
        reason="superadmin_delete",
    )
    checkout_query = SaaSCommercialCheckout.query.filter_by(lead_id=lead_id)
    checkout_count = checkout_query.count()

    task_ids = [
        row.id
        for row in db_session.query(SaaSTask.id).filter(SaaSTask.lead_id == lead_id).all()
    ]
    alert_ids = {
        row.id
        for row in SaaSAlert.query.filter(SaaSAlert.lead_id == lead_id).all()
    }
    if task_ids:
        alert_ids.update(
            row.id
            for row in SaaSAlert.query.filter(SaaSAlert.task_id.in_(task_ids)).all()
        )
    alert_count = len(alert_ids)

    recipient_ids = [
        row.id
        for row in db_session.query(SaaSCampaignRecipient.id)
        .filter(SaaSCampaignRecipient.lead_id == lead_id)
        .all()
    ]
    recipient_count = len(recipient_ids)
    campaign_event_count = (
        db_session.query(SaaSCampaignEvent).filter(
            SaaSCampaignEvent.recipient_id.in_(recipient_ids)
        ).count()
        if recipient_ids
        else 0
    )
    consent_count = SaaSLeadConsent.query.filter_by(lead_id=lead_id).count()

    # Campaign analytics tied to the deleted prospect are removed with the
    # recipient row instead of being retained as orphaned contact history.
    db_session.query(SaaSAlert).filter(SaaSAlert.lead_id == lead_id).delete(synchronize_session=False)
    if task_ids:
        db_session.query(SaaSAlert).filter(SaaSAlert.task_id.in_(task_ids)).delete(synchronize_session=False)
        db_session.query(SaaSTask).filter(SaaSTask.id.in_(task_ids)).delete(synchronize_session=False)
    if recipient_ids:
        db_session.query(SaaSCampaignEvent).filter(
            SaaSCampaignEvent.recipient_id.in_(recipient_ids)
        ).delete(synchronize_session=False)
        db_session.query(SaaSCampaignRecipient).filter(
            SaaSCampaignRecipient.id.in_(recipient_ids)
        ).delete(synchronize_session=False)
    if consent_count:
        db_session.query(SaaSLeadConsent).filter(
            SaaSLeadConsent.lead_id == lead_id
        ).delete(synchronize_session=False)

    if checkout_count:
        checkout_query.update(
            {SaaSCommercialCheckout.lead_id: None},
            synchronize_session=False,
        )

    db_session.delete(lead)
    db_session.flush()
    return {
        "lead_id": lead_id,
        "tasks_deleted": len(task_ids),
        "alerts_deleted": alert_count,
        "campaign_recipients_deleted": recipient_count,
        "campaign_events_deleted": campaign_event_count,
        "consents_deleted": consent_count,
        "checkouts_detached": checkout_count,
    }


def delete_commercial_conversation_message(db_session, *, message_id: int, company_id: int) -> dict:
    """Delete one message from the dedicated commercial CRM without touching the provider copy."""
    from stockarmobile.models.conversations import Conversation, ConversationMessage

    message = (
        db_session.query(ConversationMessage)
        .filter(
            ConversationMessage.id == int(message_id),
            ConversationMessage.company_id == int(company_id),
        )
        .first()
    )
    if message is None:
        raise ValueError("Mensaje comercial no encontrado.")

    conversation = (
        db_session.query(Conversation)
        .filter(
            Conversation.id == int(message.conversation_id),
            Conversation.company_id == int(company_id),
            Conversation.channel.in_({"email_commercial", "whatsapp_commercial"}),
        )
        .first()
    )
    if conversation is None:
        raise ValueError("El mensaje no pertenece a una conversación comercial.")

    message_metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
    if message.role == "assistant" and message_metadata.get("ai_usage_recorded") is True:
        raise ValueError("Los mensajes de IA con consumo registrado no se pueden eliminar para preservar la contabilidad de uso.")

    original_external_id = str(message.external_message_id or "").strip()[:255] or None
    email_tombstone = False

    if conversation.channel == "email_commercial" and original_external_id:
        # Work on a copy: JSON columns are not SQLAlchemy-mutable here, so mutating
        # the loaded dict in place can avoid marking the column dirty.
        metadata = dict(conversation.metadata_json) if isinstance(conversation.metadata_json, dict) else {}
        deleted_ids = metadata.get("crm_deleted_external_message_ids")
        deleted_ids = list(deleted_ids) if isinstance(deleted_ids, list) else []
        if original_external_id not in deleted_ids:
            deleted_ids.append(original_external_id)
        metadata["crm_deleted_external_message_ids"] = deleted_ids[-200:]
        conversation.metadata_json = metadata
        email_tombstone = True

    db_session.delete(message)
    db_session.flush()

    latest = (
        db_session.query(ConversationMessage)
        .filter(
            ConversationMessage.company_id == int(company_id),
            ConversationMessage.conversation_id == int(conversation.id),
        )
        .order_by(ConversationMessage.id.desc())
        .first()
    )
    conversation.updated_at = latest.created_at if latest is not None else conversation.created_at
    db_session.flush()

    return {
        "message_id": int(message_id),
        "conversation_id": int(conversation.id),
        "channel": conversation.channel,
        "email_tombstone": email_tombstone,
    }


def parse_segment_json(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    # Valores usados por la UI para significar "sin filtro" no deben
    # convertirse en filtros literales contra la base de prospectos.
    for field in ("industry", "province", "locality", "segment"):
        current = str(value.get(field) or "").strip()
        if current.lower() in {"todos", "todas", "all", "*"}:
            value[field] = ""
    return value


def _eligible_leads_query(channel: str, filters: dict):
    from app import SaaSLead

    query = SaaSLead.query.filter(SaaSLead.do_not_contact.is_(False))
    for field in ("industry", "province", "locality", "segment"):
        if filters.get(field):
            query = query.filter(getattr(SaaSLead, field).ilike(f"%{filters[field]}%"))
    if int(filters.get("min_score") or 0):
        query = query.filter(SaaSLead.lead_score >= int(filters["min_score"]))
    if channel == "email":
        # Email: usar toda la base con email válido que no haya pedido baja.
        # Los bloqueos generales y bajas explícitas siguen excluidos.
        query = query.filter(
            SaaSLead.email.isnot(None),
            SaaSLead.email_status != "invalid",
        )
        return _apply_consent_eligibility(query, SaaSLead, channel)
    if channel == "whatsapp":
        query = query.filter(SaaSLead.whatsapp.isnot(None))
        return _apply_consent_eligibility(query, SaaSLead, channel)
    raise ValueError("Canal de campaña no soportado.")


def campaign_audience_metrics(db_session, campaign) -> dict:
    """Return an auditable audience breakdown using the exact campaign filters."""
    from app import SaaSLead

    filters = parse_segment_json(campaign.segment_json)
    query = SaaSLead.query.filter(SaaSLead.do_not_contact.is_(False))
    for field in ("industry", "province", "locality", "segment"):
        if filters.get(field):
            query = query.filter(getattr(SaaSLead, field).ilike(f"%{filters[field]}%"))
    if int(filters.get("min_score") or 0):
        query = query.filter(SaaSLead.lead_score >= int(filters["min_score"]))

    total = query.count()
    blocked_query = SaaSLead.query.filter(SaaSLead.do_not_contact.is_(True))
    for field in ("industry", "province", "locality", "segment"):
        if filters.get(field):
            blocked_query = blocked_query.filter(getattr(SaaSLead, field).ilike(f"%{filters[field]}%"))
    if int(filters.get("min_score") or 0):
        blocked_query = blocked_query.filter(SaaSLead.lead_score >= int(filters["min_score"]))
    blocked = blocked_query.count()
    email_candidates = query.filter(
        SaaSLead.email.isnot(None),
        SaaSLead.email_status != "invalid",
    )
    email_available = sum(1 for lead in email_candidates.yield_per(1000) if _campaign_email_is_valid(lead))
    whatsapp_available = query.filter(
        SaaSLead.whatsapp.isnot(None),
    ).count()
    email_optin_candidates = email_candidates.filter(SaaSLead.email_consent_status == "opted_in")
    email_optin = sum(1 for lead in email_optin_candidates.yield_per(1000) if _campaign_email_is_valid(lead))
    whatsapp_optin = query.filter(
        SaaSLead.whatsapp.isnot(None),
        SaaSLead.whatsapp_consent_status == "opted_in",
    ).count()
    return {
        "total": total,
        "blocked": blocked,
        "email_available": email_available,
        "whatsapp_available": whatsapp_available,
        "email_optin": email_optin,
        "whatsapp_optin": whatsapp_optin,
        "eligible_email": (
            sum(
                1
                for lead in _eligible_leads_query("email", filters).yield_per(1000)
                if _campaign_email_is_valid(lead)
            )
            if campaign.channel in {"email", "both"} else 0
        ),
        "eligible_whatsapp": (
            _eligible_leads_query("whatsapp", filters).count()
            if campaign.channel in {"whatsapp", "both"} else 0
        ),
    }


def build_campaign_recipients(db_session, campaign_id: int) -> dict:
    from app import SaaSCampaign, SaaSCampaignEvent, SaaSCampaignRecipient, SaaSLead, utcnow

    campaign = db_session.get(SaaSCampaign, campaign_id)
    if campaign is None:
        raise ValueError("Campaña no encontrada.")
    if campaign.channel not in {"email", "whatsapp", "both"}:
        raise ValueError("Canal de campaña no soportado.")

    filters = parse_segment_json(campaign.segment_json)
    channels = ["email", "whatsapp"] if campaign.channel == "both" else [campaign.channel]
    existing = {
        (r.lead_id, r.channel)
        for r in SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
    }
    added = 0
    eligible = {}
    now = utcnow()

    for channel in channels:
        leads = _eligible_leads_query(channel, filters).order_by(
            SaaSLead.lead_score.desc(),
            SaaSLead.id.asc(),
        ).all()
        if channel == "email":
            leads = [lead for lead in leads if _campaign_email_is_valid(lead)]
        eligible[channel] = len(leads)
        for lead in leads:
            destination = normalize_email(lead.email) if channel == "email" else lead.whatsapp
            key = (lead.id, channel)
            if not destination or key in existing:
                continue
            recipient = SaaSCampaignRecipient(
                campaign_id=campaign.id,
                lead_id=lead.id,
                channel=channel,
                destination=destination,
                status="pending",
            )
            db_session.add(recipient)
            db_session.add(
                SaaSCampaignEvent(
                    campaign_id=campaign.id,
                    recipient=recipient,
                    event_type="recipient_prepared",
                    metadata_json=json.dumps(
                        {"lead_score": lead.lead_score, "channel": channel},
                        ensure_ascii=False,
                    ),
                    created_at=now,
                )
            )
            existing.add(key)
            added += 1

    campaign.target_count = len(existing)
    db_session.flush()
    return {
        "eligible": sum(eligible.values()),
        "eligible_email": eligible.get("email", 0),
        "eligible_whatsapp": eligible.get("whatsapp", 0),
        "added": added,
        "target_count": campaign.target_count,
    }


def render_merge(text: str, lead) -> str:
    rendered = text or ""
    for source, value in {
        "{{empresa}}": _clean(lead.company_name),
        "{{comercio}}": _clean(lead.company_name),
        "{{contacto}}": _clean(lead.contact_name),
        "{{rubro}}": _clean(lead.industry),
        "{{localidad}}": _clean(lead.locality),
    }.items():
        rendered = rendered.replace(source, value)
    return rendered


def _unsubscribe_url(lead) -> str:
    from flask import current_app

    token = getattr(getattr(lead, "consent", None), "unsubscribe_token", None)
    base = _clean(current_app.config.get("APP_URL")).rstrip("/")
    return f"{base}/superadmin/crm/unsubscribe/{quote(token)}" if base and token else ""


def _tracking_open_url(recipient) -> str:
    from flask import current_app

    base = _clean(current_app.config.get("APP_URL")).rstrip("/")
    return f"{base}/superadmin/crm/email/open/{quote(recipient.tracking_token)}" if base and recipient.tracking_token else ""


def _tracking_click_url(recipient, target_url: str) -> str:
    from flask import current_app

    base = _clean(current_app.config.get("APP_URL")).rstrip("/")
    if not base or not recipient.tracking_token:
        return target_url
    raw = str(target_url or "").strip()
    if raw.startswith("/"):
        target_url = f"{base}{raw}"
    return f"{base}/superadmin/crm/email/click/{quote(recipient.tracking_token)}?url={quote(target_url, safe='')}"


def _render_tracking_html(html: str, recipient) -> str:
    rendered = html or ""

    def replace_href(match):
        raw = match.group(1).strip()
        lowered = raw.lower()
        if not raw or lowered.startswith(("mailto:", "tel:", "javascript:", "#")):
            return match.group(0)
        tracked = _tracking_click_url(recipient, raw)
        return f'href="{escape(tracked, quote=True)}"'

    rendered = re.sub(r'href=["\']([^"\']+)["\']', replace_href, rendered, flags=re.IGNORECASE)
    beacon = _tracking_open_url(recipient)
    if beacon:
        rendered += (
            f'<img src="{escape(beacon, quote=True)}" width="1" height="1" '
            'alt="" style="display:block;border:0;width:1px;height:1px;">'
        )
    return rendered


def _send_email(recipient, campaign) -> tuple[bool, str]:
    from flask import current_app

    enabled = str(current_app.config.get("SAAS_MARKETING_SEND_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return False, "Envío comercial deshabilitado."
    host = _clean(current_app.config.get("SMTP_HOST"))
    user = _clean(current_app.config.get("SMTP_USER"))
    if not host or not user:
        return False, "SMTP comercial no configurado."

    if not recipient.tracking_token:
        recipient.tracking_token = secrets.token_urlsafe(32)

    msg = EmailMessage()
    msg["Subject"] = render_merge(campaign.subject, recipient.lead)[:255]
    msg["From"] = _clean(current_app.config.get("SMTP_FROM_EMAIL")) or user
    msg["To"] = recipient.destination
    msg["Reply-To"] = msg["From"]
    unsub = _unsubscribe_url(recipient.lead)
    if unsub:
        msg["List-Unsubscribe"] = f"<{unsub}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["X-StockArMobile-Campaign"] = str(campaign.id)
    msg["X-StockArMobile-Recipient"] = str(recipient.id)
    msg.set_content(render_merge(campaign.body_text or campaign.body_html, recipient.lead))
    msg.add_alternative(_render_tracking_html(render_merge(campaign.body_html, recipient.lead), recipient), subtype="html")

    try:
        with smtplib.SMTP(
            host,
            int(current_app.config.get("SMTP_PORT") or 587),
            timeout=30,
        ) as server:
            if bool(current_app.config.get("SMTP_USE_TLS", True)):
                server.starttls()
            server.login(user, current_app.config.get("SMTP_PASSWORD") or "")
            server.send_message(msg)
    except Exception:
        # The caller handles the exception and records the failure without
        # exposing SMTP credentials.
        raise
    return True, "sent"


def _whatsapp_template_parameters(campaign, lead) -> list[str]:
    from flask import current_app

    values = {
        "empresa": _clean(lead.company_name),
        "comercio": _clean(lead.company_name),
        "contacto": _clean(lead.contact_name),
        "rubro": _clean(lead.industry),
        "localidad": _clean(lead.locality),
        "trial_url": (
            f"{_clean(current_app.config.get('APP_URL')).rstrip('/')}/auth/register?selected_plan=trial"
            if _clean(current_app.config.get("APP_URL"))
            else ""
        ),
    }
    fields = [
        part.strip().lower()
        for part in _clean(campaign.whatsapp_parameter_fields).split(",")
        if part.strip()
    ]
    if fields == ["none"]:
        return []
    unknown = [field for field in fields if field not in values]
    if unknown:
        raise ValueError(f"Campos de plantilla WhatsApp no soportados: {', '.join(unknown)}")
    return [values[field] for field in fields]


def _send_whatsapp(recipient, campaign) -> tuple[bool, str, str]:
    from services.ai_agent.whatsapp_service import WhatsAppService
    from services.ai_agent.config_service import get_whatsapp_connection
    from services.saas_commercial_whatsapp import get_commercial_company

    company = get_commercial_company()
    if company is None:
        return False, "Canal WhatsApp Comercial no inicializado.", ""
    connection = get_whatsapp_connection(company)
    if not connection.get("enabled") or not connection.get("phone_number_id") or not connection.get("access_token"):
        return False, "WhatsApp Comercial no está configurado para envío.", ""

    template_name = _clean(campaign.whatsapp_template_name) or _clean(connection.get("template_name"))
    template_language = _clean(campaign.whatsapp_template_language) or _clean(connection.get("template_language")) or "es_AR"
    if not template_name:
        return False, "No hay una plantilla WhatsApp aprobada configurada.", ""

    parameters = _whatsapp_template_parameters(campaign, recipient.lead)
    result = WhatsAppService.send_template(
        company,
        to=recipient.destination,
        template_name=template_name,
        template_language=template_language,
        body_parameters=parameters,
    )
    messages = result.get("messages") if isinstance(result, dict) else None
    provider_id = ""
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        provider_id = str(messages[0].get("id") or "").strip()
    if not provider_id:
        return False, "WhatsApp API no confirmó el ID del mensaje.", ""
    return True, "sent", provider_id


def _recover_stale_campaign_claims(db_session, campaign_id: int, now) -> int:
    """Return stale recipient claims to pending so a crashed worker can recover them."""
    from app import SaaSCampaignRecipient

    cutoff = now - timedelta(minutes=30)
    query = (
        db_session.query(SaaSCampaignRecipient)
        .filter(
            SaaSCampaignRecipient.campaign_id == campaign_id,
            SaaSCampaignRecipient.status == "sending",
            SaaSCampaignRecipient.updated_at < cutoff,
        )
    )
    recovered = 0
    for recipient in query.all():
        recipient.status = "pending"
        recipient.error_reason = None
        recipient.updated_at = now
        recovered += 1
    if recovered:
        db_session.flush()
    return recovered


def _claim_campaign_recipient(db_session, recipient_id: int, now) -> bool:
    """Atomically claim one pending recipient before any external provider call."""
    from app import SaaSCampaignRecipient

    updated = (
        db_session.query(SaaSCampaignRecipient)
        .filter(
            SaaSCampaignRecipient.id == recipient_id,
            SaaSCampaignRecipient.status == "pending",
        )
        .update(
            {
                SaaSCampaignRecipient.status: "sending",
                SaaSCampaignRecipient.error_reason: "Envío en curso.",
                SaaSCampaignRecipient.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        return False
    db_session.flush()
    return True


def _finalize_campaign_status(campaign, recipients) -> str | None:
    """Set a terminal status only after all recipients leave pending/sending."""
    from app import utcnow

    pending = sum(1 for recipient in recipients if recipient.status in {"pending", "sending"})
    if pending:
        return None

    sent = sum(1 for recipient in recipients if recipient.status == "sent")
    failed = sum(1 for recipient in recipients if recipient.status == "failed")
    skipped = sum(1 for recipient in recipients if recipient.status == "skipped")

    if sent > 0 and failed == 0:
        status = "ENVIADA"
    elif sent > 0 and (failed > 0 or skipped > 0):
        status = "ENVIADA_PARCIAL"
    elif failed > 0:
        status = "FALLIDA"
    elif skipped > 0:
        status = "SIN_ENVIO"
    else:
        # No recipients means there was nothing to send.
        status = "SIN_ENVIO"

    campaign.status = status
    campaign.finished_at = utcnow()
    return status


def dispatch_due_campaigns(db_session, *, limit: int = 20, per_campaign: int = 50) -> dict:
    from app import SaaSCampaign, SaaSCampaignEvent, SaaSCampaignRecipient, utcnow
    from flask import current_app

    now = utcnow()
    marketing_enabled = str(current_app.config.get("SAAS_MARKETING_SEND_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
    campaigns = SaaSCampaign.query.filter(
        SaaSCampaign.status.in_({"APROBADA", "ENVIANDO"}),
        SaaSCampaign.scheduled_at.is_(None) | (SaaSCampaign.scheduled_at <= now),
    ).order_by(SaaSCampaign.id.asc()).limit(limit).all()
    summary = {
        "campaigns": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "disabled": 0,
        "claimed": 0,
    }

    for campaign in campaigns:
        if not marketing_enabled:
            summary["disabled"] += 1
            continue

        campaign.status = "ENVIANDO"
        campaign.started_at = campaign.started_at or now
        db_session.flush()

        _recover_stale_campaign_claims(db_session, campaign.id, now)
        db_session.commit()

        candidate_ids = [
            row.id
            for row in (
                SaaSCampaignRecipient.query
                .filter_by(campaign_id=campaign.id, status="pending")
                .order_by(SaaSCampaignRecipient.id.asc())
                .limit(per_campaign)
                .all()
            )
        ]

        for recipient_id in candidate_ids:
            claim_now = utcnow()
            if not _claim_campaign_recipient(db_session, recipient_id, claim_now):
                # Another worker claimed this recipient first.
                continue

            summary["claimed"] += 1
            db_session.commit()
            recipient = db_session.get(SaaSCampaignRecipient, recipient_id)
            if recipient is None:
                continue

            lead = recipient.lead
            try:
                if lead.do_not_contact:
                    recipient.status = "skipped"
                    recipient.error_reason = "Lead bloqueado en el momento del envío."
                    recipient.updated_at = utcnow()
                    campaign.skipped_count += 1
                    summary["skipped"] += 1
                elif recipient.channel == "email" and not _campaign_email_is_valid(lead, recipient.destination):
                    recipient.status = "skipped"
                    recipient.error_reason = "Dirección de email inválida o modificada después de preparar la audiencia."
                    recipient.updated_at = utcnow()
                    campaign.skipped_count += 1
                    summary["skipped"] += 1
                elif recipient.channel == "whatsapp" and normalize_phone(lead.whatsapp) != normalize_phone(recipient.destination):
                    recipient.status = "skipped"
                    recipient.error_reason = "Número de WhatsApp modificado después de preparar la audiencia."
                    recipient.updated_at = utcnow()
                    campaign.skipped_count += 1
                    summary["skipped"] += 1
                elif not _consent_is_eligible(
                    recipient.channel,
                    lead.email_consent_status if recipient.channel == "email" else lead.whatsapp_consent_status,
                ):
                    recipient.status = "skipped"
                    recipient.error_reason = f"Consentimiento {recipient.channel} no vigente."
                    recipient.updated_at = utcnow()
                    campaign.skipped_count += 1
                    summary["skipped"] += 1
                else:
                    if recipient.channel == "email":
                        ok, detail = _send_email(recipient, campaign)
                        provider_id = ""
                    else:
                        ok, detail, provider_id = _send_whatsapp(recipient, campaign)

                    if not ok:
                        recipient.status = "failed"
                        recipient.error_reason = detail
                        recipient.failed_at = utcnow()
                        recipient.updated_at = recipient.failed_at
                        campaign.failed_count += 1
                        summary["failed"] += 1
                    else:
                        recipient.status = "sent"
                        recipient.error_reason = None
                        recipient.provider_message_id = provider_id or recipient.provider_message_id
                        recipient.provider_status = "accepted" if provider_id else "sent"
                        recipient.sent_at = utcnow()
                        recipient.updated_at = recipient.sent_at
                        recipient.lead.last_contacted_at = recipient.sent_at
                        recipient.lead.last_contact_channel = recipient.channel
                        recipient.lead.contact_count = (recipient.lead.contact_count or 0) + 1
                        if recipient.lead.status == "nuevo":
                            recipient.lead.status = "contactado"
                        campaign.sent_count += 1
                        summary["sent"] += 1
                        db_session.add(
                            SaaSCampaignEvent(
                                campaign_id=campaign.id,
                                recipient_id=recipient.id,
                                event_type="sent",
                                metadata_json=json.dumps(
                                    {"channel": recipient.channel, "provider_message_id": provider_id},
                                    ensure_ascii=False,
                                ),
                                created_at=utcnow(),
                            )
                        )
            except Exception as exc:
                recipient.status = "failed"
                recipient.error_reason = str(exc)[:2000]
                recipient.failed_at = utcnow()
                recipient.updated_at = recipient.failed_at
                campaign.failed_count += 1
                summary["failed"] += 1
            db_session.commit()

        recipients = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        terminal_status = _finalize_campaign_status(campaign, recipients)
        campaign.target_count = len(recipients)
        db_session.commit()
        summary["campaigns"] += 1
        if terminal_status:
            summary.setdefault("final_statuses", {})[terminal_status] = summary.setdefault("final_statuses", {}).get(terminal_status, 0) + 1

    return summary

def campaign_metrics(db_session, campaign_id: int) -> dict:
    from app import SaaSCampaignRecipient

    base = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign_id)
    return {
        "total": base.count(),
        "pending": base.filter_by(status="pending").count(),
        "sent": base.filter_by(status="sent").count(),
        "failed": base.filter_by(status="failed").count(),
        "skipped": base.filter_by(status="skipped").count(),
        "opened": base.filter(SaaSCampaignRecipient.opened_at.isnot(None)).count(),
        "clicked": base.filter(SaaSCampaignRecipient.clicked_at.isnot(None)).count(),
        "delivered": base.filter(SaaSCampaignRecipient.delivered_at.isnot(None)).count(),
        "replied": base.filter(SaaSCampaignRecipient.replied_at.isnot(None)).count(),
    }



def capture_inbound_email(*, sender_email: str, subject: str = "", text: str = "", html: str = "", external_message_id: str = "", recipient_email: str = "") -> dict:
    """Register an inbound commercial email against the SaaS CRM and conversation."""
    from stockarmobile.models.conversations import Conversation, ConversationMessage, ConversationParticipant
    from app import SaaSLead, SaaSLeadConsent, db
    from services.saas_commercial_whatsapp import ensure_commercial_agent, get_commercial_company, _superadmin_actor_id

    email = str(sender_email or "").strip().lower()
    if not email or not EMAIL_RE.match(email):
        raise ValueError("El remitente del email no es válido.")
    company = get_commercial_company()
    if company is None:
        raise RuntimeError("El canal Comercial no está inicializado.")
    agent = ensure_commercial_agent(company.id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    message_id = str(external_message_id or "").strip()[:255] or None

    lead = (SaaSLead.query.filter(db.func.lower(SaaSLead.email) == email).order_by(SaaSLead.id.desc()).first())
    if lead is None:
        actor_id = _superadmin_actor_id()
        if actor_id is None:
            raise RuntimeError("No existe un usuario SuperAdmin activo para registrar el lead.")
        lead = SaaSLead(company_name="Prospecto Email", contact_name=email.split("@", 1)[0][:160], email=email[:160], source="email_comercial", status="nuevo", priority="media", notes="", whatsapp_consent_status="unknown", email_consent_status="unknown", phone_consent_status="unknown", do_not_contact=False, created_by_user_id=actor_id, company_id=None, captured_at=now, validated_at=now)
        db.session.add(lead)
        db.session.flush()
        db.session.add(SaaSLeadConsent(lead_id=lead.id, whatsapp_status="unknown", email_status="unknown", phone_status="unknown", unsubscribe_token=secrets.token_urlsafe(36), created_at=now, updated_at=now))

    external_conversation_id = f"email:{email}"
    conversation = (Conversation.query.filter_by(company_id=company.id, channel="email_commercial", external_conversation_id=external_conversation_id).order_by(Conversation.id.desc()).first())
    if conversation is None:
        conversation = Conversation(company_id=company.id, agent_id=agent.id, channel="email_commercial", external_conversation_id=external_conversation_id, status="open", metadata_json={"lead_id": lead.id, "sender_email": email, "recipient_email": str(recipient_email or "")[:160]}, created_at=now, updated_at=now)
        db.session.add(conversation)
        db.session.flush()
        db.session.add(ConversationParticipant(conversation_id=conversation.id, company_id=company.id, participant_type="prospect", participant_id=lead.id, display_name=lead.contact_name or email, created_at=now))

    if message_id:
        metadata = conversation.metadata_json if isinstance(conversation.metadata_json, dict) else {}
        deleted_ids = metadata.get("crm_deleted_external_message_ids")
        if isinstance(deleted_ids, list) and message_id in deleted_ids:
            return {"status": "ignored_deleted", "lead_id": lead.id, "conversation_id": conversation.id}

        duplicate = ConversationMessage.query.filter_by(company_id=company.id, conversation_id=conversation.id, external_message_id=message_id).first()
        if duplicate is not None:
            return {"status": "duplicate", "lead_id": lead.id, "conversation_id": conversation.id, "message_id": duplicate.id}

    body = (str(text or "").strip() or str(html or "").strip())[:20000] or "(Email sin contenido de texto)"
    content = f"Asunto: {str(subject or '').strip()[:500]}\\n\\n{body}".strip()
    message = ConversationMessage(company_id=company.id, conversation_id=conversation.id, sender_type="user", sender_id=None, role="user", content=content, content_type="email", external_message_id=message_id, idempotency_key=f"email-inbound:{message_id}" if message_id else None, metadata_json={"channel": "email_commercial", "sender_email": email, "recipient_email": str(recipient_email or "")[:160]}, created_at=now)
    db.session.add(message)
    lead.last_contacted_at = now
    lead.last_contact_channel = "email"
    lead.contact_count = int(lead.contact_count or 0) + 1
    if lead.status == "nuevo":
        lead.status = "contactado"
    lead.updated_at = now
    conversation.updated_at = now
    db.session.commit()
    return {"status": "received", "lead_id": lead.id, "conversation_id": conversation.id, "message_id": message.id}
