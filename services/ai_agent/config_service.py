"""Configuration helpers for StockARmobile AI agents and WhatsApp channels."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent

VENDOR_AGENT_NAME = "Vendedor 24 hs"
BUSINESS_AGENT_NAME = "Asistente empresarial"
COMMERCIAL_AGENT_NAME = "Comercial IA"
COMMERCIAL_COMPANY_MARKER = "whatsapp_commercial"
SPECIAL_AGENT_NAMES = {"analista": "Analista IA", "marketing": "Marketing IA"}

VENDOR_OPTION_DEFAULTS = {
    "personality": "amigable",
    "can_recommend": True,
    "can_offer_alternatives": True,
    "can_prepare_quotes": True,
    "can_take_orders": True,
    "can_follow_up": False,
    "can_handoff": False,
    "agent_name": "Vendedor IA",
    "greeting": "",
    "schedule": "24/7",
    "out_of_hours_message": "",
    "business_information": "",
    "shipping_mode": "fixed",
    "standard_shipping_cost": "0.00",
    "checkout_charges": [],
}
VENDOR_ALLOWED_PERSONALITIES = {"profesional", "amigable", "directo", "comercial"}
VENDOR_SHIPPING_MODES = {"fixed"}

SPECIAL_AGENT_OPTION_DEFAULTS = {
    "analista": {
        "default_period": "30d",
        "alerts": ["sales_drop", "critical_stock", "inactive_clients", "low_rotation"],
        "output_style": "accionable",
    },
    "marketing": {
        "default_segment": "inactivos",
        "campaign_tone": "profesional",
        "campaign_types": ["promocion", "reactivacion", "novedad"],
        "approval_required": True,
    },
}
SPECIAL_AGENT_ALLOWED_PERIODS = {"7d", "30d", "90d"}
SPECIAL_AGENT_ALLOWED_OUTPUT_STYLES = {"resumen", "accionable", "detallado"}
ANALYST_ALLOWED_ALERTS = {"sales_drop", "critical_stock", "inactive_clients", "low_rotation"}
MARKETING_ALLOWED_SEGMENTS = {"inactivos", "frecuentes", "todos"}
MARKETING_ALLOWED_TONES = {"profesional", "amigable", "directo", "comercial"}
MARKETING_ALLOWED_CAMPAIGN_TYPES = {"promocion", "reactivacion", "novedad", "stock", "fidelizacion"}



def _company_preferences(company) -> Dict[str, Any]:
    raw = getattr(company, "preferences_json", None) or ""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_company_preferences(company, payload):
    company.preferences_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _branch_preferences(branch) -> Dict[str, Any]:
    raw = getattr(branch, "vendor_ai_json", None) or ""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_branch_preferences(branch, payload: Dict[str, Any]):
    branch.vendor_ai_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def update_branch_vendor_options(company, branch_id: int, options: Dict[str, Any]):
    from app import Branch

    branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id), active=True).first()
    if branch is None:
        raise ValueError("La sucursal seleccionada no existe o está inactiva.")
    prefs = _branch_preferences(branch)
    prefs["vendor_options"] = normalize_vendor_options(options)
    save_branch_preferences(branch, prefs)
    db.session.flush()
    return prefs


def update_ai_preferences(company, *, ai_updates=None, whatsapp_updates=None):
    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"), dict) else {}
    if ai_updates:
        ai.update(ai_updates)
    whatsapp = ai.get("whatsapp") if isinstance(ai.get("whatsapp"), dict) else {}
    if whatsapp_updates:
        whatsapp.update(whatsapp_updates)
    ai["whatsapp"] = whatsapp
    prefs["ai_agent"] = ai
    save_company_preferences(company, prefs)
    return prefs


def normalize_special_options(agent_key: str, raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    key = str(agent_key or "").strip().lower()
    defaults = SPECIAL_AGENT_OPTION_DEFAULTS.get(key)
    if not defaults:
        return {}
    source = raw if isinstance(raw, dict) else {}
    options = dict(defaults)
    if key == "analista":
        period = str(source.get("default_period") or defaults["default_period"]).strip().lower()
        options["default_period"] = period if period in SPECIAL_AGENT_ALLOWED_PERIODS else defaults["default_period"]
        alerts = source.get("alerts")
        if isinstance(alerts, (list, tuple, set)):
            options["alerts"] = [
                str(item).strip().lower()
                for item in alerts
                if str(item).strip().lower() in ANALYST_ALLOWED_ALERTS
            ][:12]
        style = str(source.get("output_style") or defaults["output_style"]).strip().lower()
        options["output_style"] = style if style in SPECIAL_AGENT_ALLOWED_OUTPUT_STYLES else defaults["output_style"]
    elif key == "marketing":
        segment = str(source.get("default_segment") or defaults["default_segment"]).strip().lower()
        options["default_segment"] = segment if segment in MARKETING_ALLOWED_SEGMENTS else defaults["default_segment"]
        tone = str(source.get("campaign_tone") or defaults["campaign_tone"]).strip().lower()
        options["campaign_tone"] = tone if tone in MARKETING_ALLOWED_TONES else defaults["campaign_tone"]
        campaign_types = source.get("campaign_types")
        if isinstance(campaign_types, (list, tuple, set)):
            options["campaign_types"] = [str(item).strip().lower() for item in campaign_types if str(item).strip() in MARKETING_ALLOWED_CAMPAIGN_TYPES][:10]
        options["approval_required"] = True
    return options


def get_special_options(company, agent_key: str) -> Dict[str, Any]:
    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"), dict) else {}
    special = ai.get("special_options") if isinstance(ai.get("special_options"), dict) else {}
    raw = special.get(str(agent_key or "").strip().lower())
    return normalize_special_options(agent_key, raw)


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}
    return bool(value)


def normalize_vendor_options(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a bounded, predictable Vendor IA configuration."""
    source = raw if isinstance(raw, dict) else {}
    options = dict(VENDOR_OPTION_DEFAULTS)

    personality = str(source.get("personality") or options["personality"]).strip().lower()
    options["personality"] = personality if personality in VENDOR_ALLOWED_PERSONALITIES else options["personality"]

    for key in (
        "can_recommend",
        "can_offer_alternatives",
        "can_prepare_quotes",
        "can_take_orders",
        "can_follow_up",
        "can_handoff",
    ):
        options[key] = _coerce_bool(source.get(key), options[key])

    # El Vendedor Web utiliza una única modalidad: costo fijo configurado por el comercio.
    # Las preferencias históricas se normalizan aquí para impedir que vuelvan a activar el flujo eliminado.
    options["shipping_mode"] = "fixed"
    try:
        standard_cost = Decimal(str(source.get("standard_shipping_cost") or options["standard_shipping_cost"])).quantize(Decimal("0.01"))
    except Exception:
        standard_cost = Decimal("0.00")
    options["standard_shipping_cost"] = str(max(standard_cost, Decimal("0.00")))[:32]

    raw_charges = source.get("checkout_charges") if isinstance(source.get("checkout_charges"), list) else []
    normalized_charges = []
    allowed_types = {"fixed", "percentage"}
    allowed_bases = {"products", "products_shipping", "previous_total"}
    for index, charge in enumerate(raw_charges[:20]):
        if not isinstance(charge, dict):
            continue
        name = str(charge.get("name") or "").strip()[:120]
        if not name:
            continue
        charge_type = str(charge.get("type") or "fixed").strip().lower()
        if charge_type not in allowed_types:
            charge_type = "fixed"
        base = str(charge.get("base") or "products").strip().lower()
        if base not in allowed_bases:
            base = "products"
        try:
            value = Decimal(str(charge.get("value") or "0")).quantize(Decimal("0.01"))
        except Exception:
            value = Decimal("0.00")
        if value < Decimal("0.00"):
            value = Decimal("0.00")
        if charge_type == "percentage" and value > Decimal("100.00"):
            value = Decimal("100.00")
        normalized_charges.append({
            "id": str(charge.get("id") or f"charge-{index + 1}")[:64],
            "name": name,
            "type": charge_type,
            "value": str(value),
            "base": base,
            "active": _coerce_bool(charge.get("active"), True),
        })
    options["checkout_charges"] = normalized_charges

    for key, limit in (
        ("agent_name", 120),
        ("greeting", 1000),
        ("schedule", 120),
        ("out_of_hours_message", 1000),
        ("business_information", 4000),
    ):
        value = source.get(key)
        options[key] = str(value if value is not None else options[key]).strip()[:limit]

    return options


def get_vendor_options(company, branch_id: int | None = None) -> Dict[str, Any]:
    """Read branch-owned options; the primary branch falls back to legacy company settings."""
    if branch_id is not None:
        from app import Branch

        branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id)).first()
        if branch is not None:
            branch_prefs = _branch_preferences(branch)
            raw_branch = branch_prefs.get("vendor_options")
            if isinstance(raw_branch, dict):
                return normalize_vendor_options(raw_branch)
            if not branch.is_default:
                return normalize_vendor_options({})
    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"), dict) else {}
    raw = ai.get("vendor_options") if isinstance(ai.get("vendor_options"), dict) else {}
    return normalize_vendor_options(raw)

def save_vendor_branch_settings(company, branch_id: int, *, options=None, system_prompt=None, language=None, enabled=None):
    from app import Branch

    branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id), active=True).first()
    if branch is None:
        raise ValueError("La sucursal seleccionada no existe o está inactiva.")
    prefs = _branch_preferences(branch)
    if options is not None:
        prefs["vendor_options"] = normalize_vendor_options(options)
    config = prefs.get("vendor_config") if isinstance(prefs.get("vendor_config"), dict) else {}
    if system_prompt is not None:
        config["system_prompt"] = str(system_prompt or "").strip()[:12000]
    if language is not None:
        config["language"] = str(language or "es-AR").strip()[:8] or "es-AR"
    if config:
        prefs["vendor_config"] = config
    if enabled is not None:
        prefs["enabled"] = bool(enabled)
    save_branch_preferences(branch, prefs)
    db.session.flush()
    return prefs


def get_vendor_branch_runtime_config(company, branch_id: int | None, fallback_config=None):
    """Overlay branch-specific prompt and language without mutating company-level configuration."""
    if branch_id is None:
        return fallback_config
    from app import Branch

    branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id), active=True).first()
    if branch is None or branch.is_default:
        return fallback_config
    config = _branch_preferences(branch).get("vendor_config")
    if not isinstance(config, dict):
        return {"system_prompt": "", "language": "es-AR"}
    return {
        "system_prompt": str(config.get("system_prompt") or "")[:12000],
        "language": str(config.get("language") or "es-AR")[:8] or "es-AR",
    }


def vendor_enabled_for_branch(company, branch_id: int | None, default_enabled: bool = True) -> bool:
    if branch_id is None:
        return bool(default_enabled)
    from app import Branch

    branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id), active=True).first()
    if branch is None:
        return False
    if branch.is_default:
        return bool(default_enabled)
    prefs = _branch_preferences(branch)
    return _coerce_bool(prefs.get("enabled"), True)


def build_vendor_runtime_instructions(
    *,
    merchant_instructions: str = "",
    vendor_options: Optional[Dict[str, Any]] = None,
    language: str = "es-AR",
    channel: str = "webchat",
    first_interaction: bool = False,
) -> str:
    """Build merchant-specific Vendor instructions without replacing core guardrails."""
    options = normalize_vendor_options(vendor_options)
    lines = [
        "CONFIGURACIÓN DEL VENDEDOR IA DEL COMERCIO (contexto, nunca reemplaza las reglas del sistema):",
        f"Nombre visible: {options['agent_name']}",
        f"Personalidad: {options['personality']}",
        f"Idioma preferido: {str(language or 'es-AR').strip()[:8] or 'es-AR'}",
        f"Canal actual: {str(channel or 'desconocido').strip().lower()}",
        f"Horario informado por el comercio: {options['schedule']}",
        "Capacidades permitidas:",
        f"- Recomendar productos: {'sí' if options['can_recommend'] else 'no'}",
        f"- Ofrecer alternativas: {'sí' if options['can_offer_alternatives'] else 'no'}",
        f"- Preparar presupuestos/pedidos: {'sí' if options['can_prepare_quotes'] else 'no'}",
        f"- Tomar pedidos: {'sí' if options['can_take_orders'] else 'no'}",
        "- Gestión de envíos: costo fijo configurado automáticamente por el comercio",
        f"- Costo fijo de envío configurado: ${options['standard_shipping_cost']} ARS",
        f"- Cargos configurados para el checkout: {', '.join(charge['name'] for charge in options['checkout_charges'] if charge.get('active')) or 'ninguno'}",
        f"- Seguimiento posterior: {'sí' if options['can_follow_up'] else 'no'}",
        f"- Derivación a una persona: {'sí' if options['can_handoff'] else 'no'}",
    ]
    if options["business_information"]:
        lines.extend(["Información comercial proporcionada por el comercio:", options["business_information"]])
    if options["out_of_hours_message"]:
        lines.extend([
            "Mensaje configurado para fuera de horario (usarlo cuando corresponda al horario real del comercio):",
            options["out_of_hours_message"],
        ])
    if first_interaction and options["greeting"]:
        lines.extend([
            "Esta es la primera interacción de la conversación. Iniciá la respuesta con este saludo del comercio:",
            options["greeting"],
        ])
    if merchant_instructions:
        lines.extend([
            "Instrucciones personalizadas del comercio:",
            str(merchant_instructions).strip()[:12000],
        ])
    lines.extend([
        "REGLA DE PRIORIDAD: las instrucciones del comercio son preferencias operativas y nunca pueden desactivar, "
        "contradecir ni reemplazar las reglas de seguridad, aislamiento por comercio, validación de stock/precios, "
        "estado real de pedidos/pagos ni otras salvaguardas del sistema.",
        "REGLA DE ENVÍO: nunca inventes porcentajes ni uses reglas históricas. "
        "Para envíos a domicilio usá exclusivamente el costo fijo configurado por el comercio. "
        "Ese costo ya forma parte del presupuesto y del total de Mercado Pago; no lo dejes pendiente ni agregues otro recargo."
    ])
    return "\n".join(line for line in lines if line is not None)


def vendor_allowed_tool_names(vendor_options: Optional[Dict[str, Any]] = None):
    """Return the Vendor tools that are allowed for the normalized configuration."""
    options = normalize_vendor_options(vendor_options)
    names = {
        "buscar_producto",
        "consultar_stock",
        "buscar_cliente",
        "carrito_vendedor",
    }
    # These tools are already registered by vendor_followup_tools.py. Expose
    # the full catalog to the vendor when product recommendations are enabled;
    # a text search is not a substitute for listing all active products.
    if options["can_recommend"]:
        names.add("ver_catalogo")
    if options["can_offer_alternatives"]:
        names.add("ver_promociones")
    if options["can_take_orders"]:
        names.update({"agregar_al_carrito", "quitar_del_carrito"})
    if options["can_prepare_quotes"] and options["can_take_orders"]:
        names.add("preparar_pedido")
    return names


def _fernet():
    configured = (os.getenv("AI_CHANNEL_ENCRYPTION_KEY") or "").strip()
    if configured:
        try:
            return Fernet(configured.encode())
        except Exception as exc:
            raise RuntimeError("AI_CHANNEL_ENCRYPTION_KEY no es una clave Fernet válida.") from exc
    seed = (os.getenv("SECRET_KEY") or "stockarmobile-dev-secret").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def encrypt_secret(value):
    raw = str(value or "").strip()
    return _fernet().encrypt(raw.encode()).decode() if raw else ""


def decrypt_secret(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return _fernet().decrypt(raw.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""


def ensure_default_agents(company_id):
    agents = {}
    for name, description in (
        (
            VENDOR_AGENT_NAME,
            "Vendedor 24 hs multicanal para consultas, recomendaciones y oportunidades comerciales.",
        ),
        (
            BUSINESS_AGENT_NAME,
            "Asistente empresarial para métricas, stock, caja y gestión del negocio.",
        ),
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


def ensure_agent_for_key(company_id, agent_key):
    name = SPECIAL_AGENT_NAMES.get(str(agent_key or "").strip().lower())
    if not name:
        return None
    key = str(agent_key).strip().lower()
    description = {
        "analista": "Analista IA de ventas, stock y oportunidades.",
        "marketing": "Marketing IA para propuestas basadas en productos y clientes reales.",
    }[key]
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
    return agent


def update_special_options(company, agent_key: str, options: Optional[Dict[str, Any]] = None):
    key = str(agent_key or "").strip().lower()
    normalized = normalize_special_options(key, options)
    if not normalized:
        return _company_preferences(company)
    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"), dict) else {}
    special = ai.get("special_options") if isinstance(ai.get("special_options"), dict) else {}
    special[key] = normalized
    ai["special_options"] = special
    prefs["ai_agent"] = ai
    save_company_preferences(company, prefs)
    return prefs


def get_ai_preferences(company):
    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"), dict) else {}
    whatsapp = ai.get("whatsapp") if isinstance(ai.get("whatsapp"), dict) else {}
    return {"ai_agent": ai, "whatsapp": whatsapp}


def is_ai_enabled(company):
    global_enabled = os.getenv("AI_AGENT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not global_enabled:
        return False
    configured = get_ai_preferences(company)["ai_agent"].get("enabled")
    return _coerce_bool(configured, global_enabled)


def is_commercial_company(company) -> bool:
    prefs = _company_preferences(company)
    return str(prefs.get("internal_channel") or "").strip().lower() == COMMERCIAL_COMPANY_MARKER


def _whatsapp_token_lifecycle(data: dict) -> dict:
    """Return non-secret token health fields for UI and delivery preflight."""
    obtained_at = str(data.get("access_token_obtained_at") or "").strip()
    expires_at = str(data.get("access_token_expires_at") or "").strip()
    data_access_expires_at = str(data.get("data_access_expires_at") or "").strip()
    health = "unknown"
    days_remaining = None
    expirations = []
    for raw_expiry in (expires_at, data_access_expires_at):
        if not raw_expiry:
            continue
        try:
            expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            expirations.append(expiry)
        except (TypeError, ValueError, OverflowError):
            continue
    if expirations:
        seconds = (min(expirations) - datetime.now(timezone.utc)).total_seconds()
        days_remaining = int(max(0, seconds) // 86400)
        health = "expired" if seconds <= 0 else ("expiring" if seconds <= 14 * 86400 else "active")
    return {
        "access_token_obtained_at": obtained_at,
        "access_token_expires_at": expires_at,
        "data_access_expires_at": data_access_expires_at,
        "token_health": health,
        "token_days_remaining": days_remaining,
    }


def get_whatsapp_connection(company, branch_id: int | None = None):
    data = get_ai_preferences(company)["whatsapp"]
    if branch_id is not None:
        from app import Branch

        branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id)).first()
        if branch is not None:
            branch_ai = _branch_preferences(branch)
            branch_data = branch_ai.get("whatsapp") if isinstance(branch_ai.get("whatsapp"), dict) else {}
            if branch_data:
                data = branch_data
            elif not branch.is_default:
                return {
                    "enabled": False,
                    "phone_number_id": "",
                    "business_account_id": "",
                    "display_phone_number": "",
                    "access_token": "",
                    "template_name": "",
                    "template_language": "es_AR",
                }
    if is_commercial_company(company):
        commercial_enabled = _coerce_bool(os.getenv("WHATSAPP_COMMERCIAL_ENABLED"), False)
        commercial_phone = str(os.getenv("WHATSAPP_COMMERCIAL_PHONE_NUMBER_ID") or "").strip()
        if commercial_enabled and commercial_phone:
            return {
                "enabled": True,
                "phone_number_id": commercial_phone,
                "business_account_id": str(os.getenv("WHATSAPP_COMMERCIAL_WABA_ID") or "").strip(),
                "display_phone_number": str(os.getenv("WHATSAPP_COMMERCIAL_DISPLAY_PHONE_NUMBER") or "").strip(),
                "access_token": str(os.getenv("WHATSAPP_COMMERCIAL_ACCESS_TOKEN") or "").strip(),
                "template_name": str(os.getenv("WHATSAPP_COMMERCIAL_TEMPLATE_NAME") or "").strip(),
                "template_language": str(os.getenv("WHATSAPP_COMMERCIAL_TEMPLATE_LANGUAGE") or "es_AR").strip(),
                **_whatsapp_token_lifecycle({"access_token_expires_at": os.getenv("WHATSAPP_COMMERCIAL_ACCESS_TOKEN_EXPIRES_AT", "")}),
            }
    return {
        "enabled": bool(data.get("enabled", False)),
        "phone_number_id": str(data.get("phone_number_id") or "").strip(),
        "business_account_id": str(data.get("business_account_id") or "").strip(),
        "display_phone_number": str(data.get("display_phone_number") or "").strip(),
        "access_token": decrypt_secret(data.get("access_token_encrypted")),
        "template_name": str(data.get("template_name") or "").strip(),
        "template_language": str(data.get("template_language") or "es_AR").strip(),
        **_whatsapp_token_lifecycle(data),
    }


def _whatsapp_registration_branch_id(company, branch_id):
    if branch_id is not None:
        return int(branch_id)
    from stockarmobile.branch_context import get_default_branch_id
    return get_default_branch_id(int(company.id))


def _validate_whatsapp_phone_owner(company, branch_id, phone_number_id):
    target = str(phone_number_id or "").strip()
    if not target:
        return _whatsapp_registration_branch_id(company, branch_id)
    from stockarmobile.models.conversations import WhatsAppChannelRegistration
    requested_branch_id = _whatsapp_registration_branch_id(company, branch_id)
    registration = db.session.query(WhatsAppChannelRegistration).filter_by(phone_number_id=target).first()
    if registration is not None and (
        int(registration.company_id) != int(company.id)
        or (registration.branch_id or None) != (requested_branch_id or None)
    ):
        raise ValueError("Este número de WhatsApp ya está conectado a otra empresa o sucursal.")

    # Existing installations may have JSON configuration written before the registry existed.
    legacy_company, legacy_branch = company_branch_for_whatsapp_phone_id(target)
    if legacy_company is not None:
        legacy_branch_id = int(legacy_branch.id) if legacy_branch is not None else None
        if (
            int(legacy_company.id) != int(company.id)
            or (legacy_branch_id or None) != (requested_branch_id or None)
        ):
            raise ValueError("Este número de WhatsApp ya está conectado a otra empresa o sucursal.")
    return requested_branch_id


def _sync_whatsapp_phone_registration(company, branch_id, previous_phone_id, phone_number_id):
    from stockarmobile.models.conversations import WhatsAppChannelRegistration
    scope_branch_id = _whatsapp_registration_branch_id(company, branch_id)
    previous = str(previous_phone_id or "").strip()
    target = str(phone_number_id or "").strip()
    if previous and previous != target:
        old_registration = db.session.query(WhatsAppChannelRegistration).filter_by(phone_number_id=previous).first()
        if old_registration is not None and (
            int(old_registration.company_id) == int(company.id)
            and (old_registration.branch_id or None) == (scope_branch_id or None)
        ):
            db.session.delete(old_registration)
    if target:
        registration = (
            db.session.query(WhatsAppChannelRegistration)
            .filter_by(phone_number_id=target)
            .with_for_update()
            .first()
        )
        if registration is not None and (
            int(registration.company_id) != int(company.id)
            or (registration.branch_id or None) != (scope_branch_id or None)
        ):
            raise ValueError("Este número de WhatsApp ya está conectado a otra empresa o sucursal.")
        if registration is None:
            db.session.add(WhatsAppChannelRegistration(
                phone_number_id=target,
                company_id=int(company.id),
                branch_id=scope_branch_id,
            ))
    db.session.flush()


def configure_whatsapp_connection(
    company,
    *,
    phone_number_id,
    access_token=None,
    business_account_id="",
    display_phone_number="",
    enabled=True,
    template_name="",
    template_language="es_AR",
    branch_id: int | None = None,
    whatsapp_updates: Optional[Dict[str, Any]] = None,
):
    if branch_id is not None:
        from app import Branch

        branch = Branch.query.filter_by(id=int(branch_id), company_id=int(company.id), active=True).first()
        if branch is None:
            raise ValueError("La sucursal seleccionada no existe o está inactiva.")
        prefs = _branch_preferences(branch)
        whatsapp = prefs.get("whatsapp") if isinstance(prefs.get("whatsapp"), dict) else {}
        previous_phone_id = str(whatsapp.get("phone_number_id") or "").strip()
        target_branch_id = _validate_whatsapp_phone_owner(company, branch.id, phone_number_id)
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
        if whatsapp_updates:
            whatsapp.update(whatsapp_updates)
        prefs["whatsapp"] = whatsapp
        save_branch_preferences(branch, prefs)
        _sync_whatsapp_phone_registration(company, target_branch_id, previous_phone_id, phone_number_id)
        return

    prefs = _company_preferences(company)
    ai = prefs.get("ai_agent") if isinstance(prefs.get("ai_agent"), dict) else {}
    whatsapp = ai.get("whatsapp") if isinstance(ai.get("whatsapp"), dict) else {}
    previous_phone_id = str(whatsapp.get("phone_number_id") or "").strip()
    target_branch_id = _validate_whatsapp_phone_owner(company, None, phone_number_id)
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
    if whatsapp_updates:
        whatsapp.update(whatsapp_updates)
    ai["whatsapp"] = whatsapp
    prefs["ai_agent"] = ai
    save_company_preferences(company, prefs)
    _sync_whatsapp_phone_registration(company, target_branch_id, previous_phone_id, phone_number_id)


def company_branch_for_whatsapp_phone_id(phone_number_id):
    from app import Branch, Company

    target = str(phone_number_id or "").strip()
    if not target:
        return None, None
    from stockarmobile.models.conversations import WhatsAppChannelRegistration
    registration = db.session.query(WhatsAppChannelRegistration).filter_by(phone_number_id=target).first()
    if registration is not None:
        company = Company.query.filter_by(id=registration.company_id, active=True).first()
        branch = Branch.query.filter_by(
            id=registration.branch_id, company_id=registration.company_id
        ).first() if registration.branch_id else None
        if company is not None:
            return company, branch
    candidates = (
        Branch.query.join(Company, Company.id == Branch.company_id)
        .filter(Company.active.is_(True), Branch.active.is_(True), Branch.vendor_ai_json.contains(target))
        .order_by(Branch.company_id.asc(), Branch.id.asc())
        .all()
    )
    for branch in candidates:
        company = Company.query.filter_by(id=branch.company_id, active=True).first()
        if company and get_whatsapp_connection(company, branch_id=branch.id)["phone_number_id"] == target:
            return company, branch

    candidates = (
        Company.query.filter(Company.active.is_(True), Company.preferences_json.contains(target))
        .order_by(Company.id.asc())
        .all()
    )
    for company in candidates:
        connection = get_whatsapp_connection(company)
        if connection["phone_number_id"] == target:
            from stockarmobile.branch_context import get_default_branch_id
            branch_id = get_default_branch_id(company.id)
            from app import Branch
            branch = Branch.query.filter_by(id=branch_id, company_id=company.id).first() if branch_id else None
            return company, branch
    return None, None


def company_for_whatsapp_phone_id(phone_number_id):
    company, _branch = company_branch_for_whatsapp_phone_id(phone_number_id)
    return company


def choose_agent(company_id, *, channel):
    agents = ensure_default_agents(company_id)
    return agents[VENDOR_AGENT_NAME] if str(channel or "").strip().lower() in {"whatsapp", "webchat"} else agents[BUSINESS_AGENT_NAME]
