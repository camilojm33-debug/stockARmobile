"""Commercial acquisition CRM services for SuperAdmin."""
from __future__ import annotations

import csv
import json
import re
import secrets
import smtplib
from email.message import EmailMessage
from html import escape
from io import BytesIO, StringIO
from urllib.parse import quote

from openpyxl import load_workbook

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
MAX_IMPORT_ROWS = 10000
VALID_CHANNELS = {"email", "whatsapp", "both"}
CONSENT_VALUES = {"opted_in", "opted_out", "unknown"}

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
    return {
        "industry": _clean(request.form.get("industry") or request.args.get("industry")),
        "province": _clean(request.form.get("province") or request.args.get("province")),
        "locality": _clean(request.form.get("locality") or request.args.get("locality")),
        "segment": _clean(request.form.get("segment") or request.args.get("segment")),
        "min_score": score,
    }


def delete_saas_lead(db_session, lead_id: int) -> dict:
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


def parse_segment_json(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _eligible_leads_query(channel: str, filters: dict):
    from app import SaaSLead

    query = SaaSLead.query.filter(SaaSLead.do_not_contact.is_(False))
    for field in ("industry", "province", "locality", "segment"):
        if filters.get(field):
            query = query.filter(getattr(SaaSLead, field).ilike(f"%{filters[field]}%"))
    if int(filters.get("min_score") or 0):
        query = query.filter(SaaSLead.lead_score >= int(filters["min_score"]))
    if channel == "email":
        return query.filter(
            SaaSLead.email.isnot(None),
            SaaSLead.email_status != "invalid",
            SaaSLead.email_consent_status == "opted_in",
        )
    if channel == "whatsapp":
        return query.filter(
            SaaSLead.whatsapp.isnot(None),
            SaaSLead.whatsapp_consent_status == "opted_in",
        )
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
    email_available = query.filter(
        SaaSLead.email.isnot(None),
        SaaSLead.email_status != "invalid",
    ).count()
    whatsapp_available = query.filter(
        SaaSLead.whatsapp.isnot(None),
    ).count()
    email_optin = query.filter(SaaSLead.email_consent_status == "opted_in").count()
    whatsapp_optin = query.filter(SaaSLead.whatsapp_consent_status == "opted_in").count()
    return {
        "total": total,
        "blocked": blocked,
        "email_available": email_available,
        "whatsapp_available": whatsapp_available,
        "email_optin": email_optin,
        "whatsapp_optin": whatsapp_optin,
        "eligible_email": email_optin if campaign.channel in {"email", "both"} else 0,
        "eligible_whatsapp": whatsapp_optin if campaign.channel in {"whatsapp", "both"} else 0,
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
        eligible[channel] = len(leads)
        for lead in leads:
            destination = lead.email if channel == "email" else lead.whatsapp
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
    return True, "sent", provider_id


def dispatch_due_campaigns(db_session, *, limit: int = 20, per_campaign: int = 50) -> dict:
    from app import SaaSCampaign, SaaSCampaignEvent, SaaSCampaignRecipient, utcnow
    from flask import current_app

    now = utcnow()
    marketing_enabled = str(current_app.config.get("SAAS_MARKETING_SEND_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
    campaigns = SaaSCampaign.query.filter(
        SaaSCampaign.status.in_({"APROBADA", "ENVIANDO"}),
        SaaSCampaign.scheduled_at.is_(None) | (SaaSCampaign.scheduled_at <= now),
    ).order_by(SaaSCampaign.id.asc()).limit(limit).all()
    summary = {"campaigns": 0, "sent": 0, "failed": 0, "skipped": 0, "disabled": 0}

    for campaign in campaigns:
        if not marketing_enabled:
            summary["disabled"] += 1
            continue

        campaign.status = "ENVIANDO"
        campaign.started_at = campaign.started_at or now
        db_session.flush()
        recipients = (
            SaaSCampaignRecipient.query
            .filter_by(campaign_id=campaign.id, status="pending")
            .order_by(SaaSCampaignRecipient.id.asc())
            .limit(per_campaign)
            .all()
        )

        for recipient in recipients:
            lead = recipient.lead
            if lead.do_not_contact:
                recipient.status = "skipped"
                recipient.error_reason = "Lead bloqueado en el momento del envío."
                recipient.updated_at = utcnow()
                campaign.skipped_count += 1
                summary["skipped"] += 1
                continue

            if recipient.channel == "email" and lead.email_consent_status != "opted_in":
                recipient.status = "skipped"
                recipient.error_reason = "Consentimiento email no vigente."
                recipient.updated_at = utcnow()
                campaign.skipped_count += 1
                summary["skipped"] += 1
                continue

            if recipient.channel == "whatsapp" and lead.whatsapp_consent_status != "opted_in":
                recipient.status = "skipped"
                recipient.error_reason = "Consentimiento WhatsApp no vigente."
                recipient.updated_at = utcnow()
                campaign.skipped_count += 1
                summary["skipped"] += 1
                continue

            try:
                if recipient.channel == "email":
                    ok, detail = _send_email(recipient, campaign)
                    provider_id = ""
                else:
                    ok, detail, provider_id = _send_whatsapp(recipient, campaign)

                if not ok:
                    recipient.status = "failed"
                    recipient.error_reason = detail
                    recipient.failed_at = utcnow()
                    campaign.failed_count += 1
                    summary["failed"] += 1
                else:
                    recipient.status = "sent"
                    recipient.provider_message_id = provider_id or recipient.provider_message_id
                    recipient.provider_status = "accepted" if provider_id else "sent"
                    recipient.sent_at = utcnow()
                    recipient.lead.last_contacted_at = recipient.sent_at
                    recipient.lead.last_contact_channel = recipient.channel
                    recipient.lead.contact_count = (recipient.lead.contact_count or 0) + 1
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
                campaign.failed_count += 1
                summary["failed"] += 1
            db_session.flush()

        remaining = SaaSCampaignRecipient.query.filter_by(
            campaign_id=campaign.id, status="pending"
        ).count()
        if remaining == 0:
            campaign.status = "ENVIADA"
            campaign.finished_at = utcnow()
        campaign.target_count = SaaSCampaignRecipient.query.filter_by(
            campaign_id=campaign.id
        ).count()
        db_session.flush()
        summary["campaigns"] += 1

    db_session.commit()
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
