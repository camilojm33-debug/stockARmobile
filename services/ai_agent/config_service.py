"""Configuration helpers for StockARmobile AI agents and WhatsApp channels."""
from __future__ import annotations
import base64, hashlib, json, os
from typing import Any, Dict, Optional
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, has_app_context
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent
VENDOR_AGENT_NAME="Vendedor 24 hs"
BUSINESS_AGENT_NAME="Asistente empresarial"

def _company_preferences(company)->Dict[str,Any]:
    raw=getattr(company,"preferences_json",None) or ""
    if not isinstance(raw,str) or not raw.strip(): return {}
    try: payload=json.loads(raw)
    except json.JSONDecodeError: return {}
    return payload if isinstance(payload,dict) else {}

def save_company_preferences(company,payload): company.preferences_json=json.dumps(payload,ensure_ascii=False,separators=(",",":"))
def update_ai_preferences(company,*,ai_updates=None,whatsapp_updates=None):
    prefs=_company_preferences(company); ai=prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"),dict) else {}
    if ai_updates: ai.update(ai_updates)
    whatsapp=ai.get("whatsapp") if isinstance(ai.get("whatsapp"),dict) else {}
    if whatsapp_updates: whatsapp.update(whatsapp_updates)
    ai["whatsapp"]=whatsapp; prefs["ai_agent"]=ai; save_company_preferences(company,prefs); return prefs

def _fernet():
    configured=(os.getenv("AI_CHANNEL_ENCRYPTION_KEY") or "").strip()
    if configured:
        try: return Fernet(configured.encode())
        except Exception as exc: raise RuntimeError("AI_CHANNEL_ENCRYPTION_KEY no es una clave Fernet válida.") from exc
    if has_app_context() and current_app.config.get("IS_PRODUCTION_ENV"):
        raise RuntimeError("AI_CHANNEL_ENCRYPTION_KEY es obligatoria en producción.")
    if (os.getenv("FLASK_ENV") or os.getenv("APP_ENV") or "").strip().lower() == "production":
        raise RuntimeError("AI_CHANNEL_ENCRYPTION_KEY es obligatoria en producción.")
    seed=(os.getenv("SECRET_KEY") or "stockarmobile-dev-secret").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))

def encrypt_secret(value):
    raw=str(value or "").strip(); return _fernet().encrypt(raw.encode()).decode() if raw else ""
def decrypt_secret(value):
    raw=str(value or "").strip()
    if not raw: return ""
    try: return _fernet().decrypt(raw.encode()).decode()
    except (InvalidToken,ValueError,TypeError): return ""

def ensure_default_agents(company_id):
    agents={}
    for name,description in ((VENDOR_AGENT_NAME,"Vendedor 24 hs por WhatsApp para consultas y oportunidades comerciales."),(BUSINESS_AGENT_NAME,"Asistente empresarial para métricas, stock, caja y gestión del negocio.")):
        agent=db.session.query(Agent).filter(Agent.company_id==company_id,Agent.name==name).order_by(Agent.id.asc()).first()
        if agent is None:
            agent=Agent(company_id=company_id,name=name,description=description,active=True); db.session.add(agent); db.session.flush()
        agents[name]=agent
    return agents

def get_ai_preferences(company):
    prefs=_company_preferences(company); ai=prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"),dict) else {}; whatsapp=ai.get("whatsapp") if isinstance(ai.get("whatsapp"),dict) else {}
    return {"ai_agent":ai,"whatsapp":whatsapp}

def is_ai_enabled(company):
    configured=get_ai_preferences(company)["ai_agent"].get("enabled")
    return bool(configured) if configured is not None else os.getenv("AI_AGENT_ENABLED","true").strip().lower() in {"1","true","yes","on"}

def get_whatsapp_connection(company):
    data=get_ai_preferences(company)["whatsapp"]
    return {"enabled":bool(data.get("enabled",False)),"phone_number_id":str(data.get("phone_number_id") or "").strip(),"business_account_id":str(data.get("business_account_id") or "").strip(),"display_phone_number":str(data.get("display_phone_number") or "").strip(),"access_token":decrypt_secret(data.get("access_token_encrypted")),"template_name":str(data.get("template_name") or "").strip(),"template_language":str(data.get("template_language") or "es_AR").strip()}

def configure_whatsapp_connection(company,*,phone_number_id,access_token=None,business_account_id="",display_phone_number="",enabled=True,template_name="",template_language="es_AR"):
    prefs=_company_preferences(company); ai=prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"),dict) else {}; whatsapp=ai.get("whatsapp") if isinstance(ai.get("whatsapp"),dict) else {}
    whatsapp.update({"enabled":bool(enabled),"phone_number_id":str(phone_number_id or "").strip(),"business_account_id":str(business_account_id or "").strip(),"display_phone_number":str(display_phone_number or "").strip(),"template_name":str(template_name or "").strip(),"template_language":str(template_language or "es_AR").strip() or "es_AR"})
    if access_token: whatsapp["access_token_encrypted"]=encrypt_secret(access_token)
    ai["whatsapp"]=whatsapp; prefs["ai_agent"]=ai; save_company_preferences(company,prefs)

def company_for_whatsapp_phone_id(phone_number_id):
    from app import Company
    target=str(phone_number_id or "").strip()
    if not target: return None
    candidates=Company.query.filter(Company.active.is_(True),Company.preferences_json.contains(target)).order_by(Company.id.asc()).all()
    for company in candidates:
        if get_whatsapp_connection(company)["phone_number_id"] == target: return company
    return None

def choose_agent(company_id,*,channel):
    agents=ensure_default_agents(company_id)
    return agents[VENDOR_AGENT_NAME] if channel=="whatsapp" else agents[BUSINESS_AGENT_NAME]
