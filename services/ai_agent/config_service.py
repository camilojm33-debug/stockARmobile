"""Configuration helpers for StockARmobile AI agents and WhatsApp channels."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent


VENDOR_AGENT_NAME = "Vendedor 24 hs"
BUSINESS_AGENT_NAME = "Asistente empresarial"


def _company_preferences(company) -> Dict[str, Any]:
    raw = getattr(company, "preferences_json", None) or ""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_company_preferences(company, payload: Dict[str, Any]) -> None:
    company.preferences_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fernet() -> Fernet:
    configured = (os.getenv("AI_CHANNEL_ENCRYPTION_KEY") or "").strip()
    if configured:
        try:
            return Fernet(configured.encode("utf-8"))
        except Exception as exc:
            raise RuntimeError("AI_CHANNEL_ENCRYPTION_KEY no es una clave Fernet válida.") from exc

    # Development-safe deterministic fallback. Production must configure a real key.
    seed = (os.getenv("SECRET_KEY") or "stockarmobile-dev-secret").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return _fernet().decrypt(raw.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def ensure_default_agents(company_id: int) -> Dict[str, Agent]:
    """Create the two first-class agents lazily and return them."""
    agents: Dict[str, Agent] = {}
    for name, description in (
        (VENDOR_AGENT_NAME, "Vendedor 24 hs por WhatsApp para consultas y oportunidades comerciales."),
        (BUSINESS_AGENT_NAME, "Asistente empresarial para métricas, stock, caja y gestión del negocio."),
    ):
        agent = (
            db.session.query(Agent)
            .filter(Agent.company_id == company_id, Agent.name == name)
            .order_by(Agent.id.asc())
            .first()
        )
        if agent is None:
            agent = Agent(company_id=company_id, name=name, description=description, active=True)
            db.session.add(agent)
            db.session.flush()
        agents[name] = agent
    return agents


def get_ai_preferences(company) -> Dict[str, Any]:
    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent")
    if not isinstance(ai, dict):
        ai = {}
    whatsapp = ai.get("whatsapp")
    if not isinstance(whatsapp, dict):
        whatsapp = {}
    return {"ai_agent": ai, "whatsapp": whatsapp}


def is_ai_enabled(company) -> bool:
    ai = get_ai_preferences(company)["ai_agent"]
    configured = ai.get("enabled")
    if configured is not None:
        return bool(configured)
    return os.getenv("AI_AGENT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def get_whatsapp_connection(company) -> Dict[str, Any]:
    data = get_ai_preferences(company)["whatsapp"]
    return {
        "enabled": bool(data.get("enabled", False)),
        "phone_number_id": str(data.get("phone_number_id") or "").strip(),
        "business_account_id": str(data.get("business_account_id") or "").strip(),
        "display_phone_number": str(data.get("display_phone_number") or "").strip(),
        "access_token": decrypt_secret(data.get("access_token_encrypted")),
        "template_name": str(data.get("template_name") or "").strip(),
        "template_language": str(data.get("template_language") or "es_AR").strip(),
    }


def configure_whatsapp_connection(
    company,
    *,
    phone_number_id: str,
    access_token: Optional[str] = None,
    business_account_id: str = "",
    display_phone_number: str = "",
    enabled: bool = True,
    template_name: str = "",
    template_language: str = "es_AR",
) -> None:
    prefs = _company_preferences(company)
    ai = prefs.setdefault("ai_agent", {})
    whatsapp = ai.setdefault("whatsapp", {})
    whatsapp.update(
        {
            "enabled": bool(enabled),
            "phone_number_id": str(phone_number_id or "").strip(),
            "business_account_id": str(business_account_id or "").strip(),
            "display_phone_number": str(display_phone_number or "").strip(),
            "template_name": str(template_name or "").strip(),
            "template_language": str(template_language or "es_AR").strip() or "es_AR",
        }
    )
    if access_token:
        whatsapp["access_token_encrypted"] = encrypt_secret(access_token)
    save_company_preferences(company, prefs)


def company_for_whatsapp_phone_id(phone_number_id: str):
    from app import Company

    target = str(phone_number_id or "").strip()
    if not target:
        return None

    # PostgreSQL and SQLite both support substring matching on TEXT columns.
    company = (
        Company.query
        .filter(Company.active.is_(True), Company.preferences_json.contains(target))
        .order_by(Company.id.asc())
        .first()
    )
    return company


def choose_agent(company_id: int, *, channel: str):
    agents = ensure_default_agents(company_id)
    if channel == "whatsapp":
        return agents[VENDOR_AGENT_NAME]
    return agents[BUSINESS_AGENT_NAME]
