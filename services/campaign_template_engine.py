"""Shared, metadata-driven campaign template engine for StockARmobile.

This module centralizes campaign goals, audience scopes, variable schemas,
defaults, parameter validation and positional template rendering. The two CRM
adapters deliberately keep their own recipient tables, WhatsApp credentials,
consent rules and dispatch workers:
- SuperAdmin campaigns operate on SaaSLead prospects only.
- Merchant campaigns operate on Clients belonging to one company only.
"""
from __future__ import annotations

import json
import re
from typing import Any

POSITIONAL_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")

SCOPE_SAAS_PROSPECTS = "saas_prospects"
SCOPE_MERCHANT_CLIENTS = "merchant_clients"

# These are CRM data keys, not Meta parameter names. Meta receives an ordered
# list according to the selected template's {{1}}..{{N}} BODY variables.
VARIABLES = {
    SCOPE_SAAS_PROSPECTS: (
        {"key": "contacto", "label": "Nombre del contacto", "description": "Persona de contacto del prospecto."},
        {"key": "empresa", "label": "Nombre de la empresa", "description": "Nombre legal/comercial del prospecto."},
        {"key": "comercio", "label": "Nombre del comercio", "description": "Nombre del comercio prospecto."},
        {"key": "rubro", "label": "Rubro", "description": "Actividad comercial registrada."},
        {"key": "localidad", "label": "Localidad", "description": "Localidad del prospecto."},
        {"key": "producto", "label": "Producto StockARmobile", "description": "Nombre del producto/plataforma ofrecida."},
        {"key": "mensaje", "label": "Mensaje de campaña", "description": "Texto de la propuesta comercial."},
        {"key": "asunto", "label": "Asunto / título", "description": "Asunto de la campaña."},
        {"key": "trial_url", "label": "Enlace de prueba", "description": "URL pública para iniciar la prueba."},
    ),
    SCOPE_MERCHANT_CLIENTS: (
        {"key": "contacto", "label": "Nombre del cliente", "description": "Nombre de la ficha de cliente."},
        {"key": "empresa", "label": "Nombre del comercio emisor", "description": "Nombre de la empresa del usuario."},
        {"key": "comercio", "label": "Nombre del comercio emisor", "description": "Nombre de la empresa del usuario."},
        {"key": "telefono", "label": "WhatsApp / teléfono", "description": "Número del contacto."},
        {"key": "email", "label": "Email del cliente", "description": "Correo registrado en la ficha."},
        {"key": "localidad", "label": "Localidad", "description": "Localidad del cliente."},
        {"key": "provincia", "label": "Provincia", "description": "Provincia del cliente."},
        {"key": "rubro", "label": "Tipo de cliente", "description": "Minorista, mayorista u otro tipo registrado."},
        {"key": "producto", "label": "Producto", "description": "Producto asociado a la campaña o valor predeterminado."},
        {"key": "precio", "label": "Precio del producto", "description": "Precio del producto asociado."},
        {"key": "mensaje", "label": "Mensaje de campaña", "description": "Texto editable de la campaña."},
        {"key": "asunto", "label": "Objetivo / asunto", "description": "Objetivo o título de la campaña."},
    ),
}

GOAL_LIBRARY = {
    SCOPE_SAAS_PROSPECTS: (
        {
            "key": "saas_acquisition",
            "label": "Captación de nuevos comercios",
            "description": "Presentar StockARmobile a prospectos comerciales nuevos.",
            "default_mapping": ["contacto", "empresa", "producto", "mensaje"],
            "default_values": {"producto": "StockARmobile"},
        },
        {
            "key": "saas_trial_conversion",
            "label": "Conversión a prueba / registro",
            "description": "Guiar prospectos hacia el registro y activación de una prueba.",
            "default_mapping": ["contacto", "empresa", "mensaje", "trial_url"],
            "default_values": {"producto": "StockARmobile"},
        },
        {
            "key": "saas_lead_followup",
            "label": "Seguimiento de prospectos",
            "description": "Retomar conversaciones comerciales con prospectos existentes.",
            "default_mapping": ["contacto", "empresa", "mensaje", "asunto"],
            "default_values": {"producto": "StockARmobile"},
        },
    ),
    SCOPE_MERCHANT_CLIENTS: (
        {
            "key": "customer_marketing",
            "label": "Promoción para clientes",
            "description": "Comunicar ofertas, productos y novedades a clientes del comercio.",
            "master_template_key": "marketing_general",
            "default_mapping": ["contacto", "producto", "mensaje"],
            "default_values": {"producto": "Novedades de nuestro comercio"},
        },
        {
            "key": "customer_reactivation",
            "label": "Recuperar clientes inactivos",
            "description": "Invitar a volver a clientes que no compran hace un tiempo.",
            "master_template_key": "customer_reactivation",
            "default_mapping": ["contacto", "producto", "mensaje"],
            "default_values": {"producto": "Una propuesta especial para vos"},
        },
        {
            "key": "customer_loyalty",
            "label": "Fidelización",
            "description": "Agradecer la preferencia y fortalecer la relación con clientes.",
            "master_template_key": "customer_loyalty",
            "default_mapping": ["contacto", "producto", "mensaje"],
            "default_values": {"producto": "Beneficios para clientes"},
        },
        {
            "key": "customer_new_product",
            "label": "Nuevos productos y novedades",
            "description": "Presentar lanzamientos o productos nuevos del comercio.",
            "master_template_key": "customer_new_product",
            "default_mapping": ["contacto", "producto", "mensaje"],
            "default_values": {"producto": "Nuevo producto"},
        },
    ),
}


def goal_options(scope: str) -> list[dict]:
    """Return non-secret goal/preset metadata for one strictly isolated CRM scope."""
    return [dict(item) for item in GOAL_LIBRARY.get(str(scope or ""), ())]


def get_goal(scope: str, goal_key: str | None) -> dict:
    options = GOAL_LIBRARY.get(str(scope or ""), ())
    requested = str(goal_key or "").strip()
    for item in options:
        if item["key"] == requested:
            return dict(item)
    if options:
        return dict(options[0])
    raise ValueError("El tipo de campaña no está habilitado para esta audiencia.")


def variable_options(scope: str) -> list[dict]:
    return [dict(item) for item in VARIABLES.get(str(scope or ""), ())]


def allowed_variable_keys(scope: str) -> set[str]:
    return {item["key"] for item in VARIABLES.get(str(scope or ""), ())}


def normalize_mapping(scope: str, mapping: Any, *, expected_count: int | None = None,
                      goal_key: str | None = None) -> tuple[list[str], list[str]]:
    """Normalize and validate an ordered mapping. Returns (fields, errors)."""
    if isinstance(mapping, str):
        fields = [part.strip().lower() for part in mapping.split(",") if part.strip()]
    elif isinstance(mapping, (list, tuple)):
        fields = [str(part or "").strip().lower() for part in mapping if str(part or "").strip()]
    else:
        fields = []
    if not fields:
        fields = list(get_goal(scope, goal_key).get("default_mapping") or [])
    allowed = allowed_variable_keys(scope)
    errors = []
    unknown = [field for field in fields if field not in allowed]
    if unknown:
        errors.append("Variables no soportadas para esta audiencia: " + ", ".join(sorted(set(unknown))) + ".")
    if len(fields) != len(set(fields)):
        errors.append("No repitas la misma variable en más de un parámetro.")
    if expected_count is not None and len(fields) != max(0, int(expected_count)):
        errors.append(
            f"La plantilla requiere {int(expected_count)} parámetro(s), pero el mapeo tiene {len(fields)}. "
            "Asigná un campo a cada variable de Meta."
        )
    return fields, errors


def parse_defaults(scope: str, raw: Any) -> dict[str, str]:
    """Parse bounded, non-secret per-campaign fallbacks for known variable keys."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    allowed = allowed_variable_keys(scope)
    result = {}
    for key, value in raw.items():
        normalized = str(key or "").strip().lower()
        text = str(value or "").strip()
        if normalized in allowed and text:
            result[normalized] = text[:300]
    return result


def campaign_settings(scope: str, settings: Any) -> dict:
    """Read the scope, goal and variable defaults stored in a campaign JSON snapshot."""
    if isinstance(settings, str):
        try:
            settings = json.loads(settings or "{}")
        except (TypeError, ValueError):
            settings = {}
    settings = settings if isinstance(settings, dict) else {}
    goal = get_goal(scope, settings.get("campaign_goal"))
    defaults = dict(goal.get("default_values") or {})
    defaults.update(parse_defaults(scope, settings.get("parameter_defaults")))
    return {
        "audience_scope": scope,
        "campaign_goal": goal["key"],
        "parameter_defaults": defaults,
        "whatsapp_parameter_fields": settings.get("whatsapp_parameter_fields") or goal.get("default_mapping"),
    }


def resolve_parameters(scope: str, mapping: Any, context: dict, defaults: Any = None,
                       *, expected_count: int | None = None,
                       goal_key: str | None = None) -> dict:
    """Resolve positional Meta parameters and expose every missing value explicitly."""
    fields, mapping_errors = normalize_mapping(
        scope, mapping, expected_count=expected_count, goal_key=goal_key
    )
    safe_context = context if isinstance(context, dict) else {}
    safe_defaults = parse_defaults(scope, defaults)
    values = []
    details = []
    missing = []
    for index, field in enumerate(fields, start=1):
        value = str(safe_context.get(field) or "").strip()
        source = "contacto/campaña"
        if not value:
            value = safe_defaults.get(field, "")
            source = "valor predeterminado" if value else "sin dato"
        if not value:
            missing.append(field)
        values.append(value[:1024])
        details.append({
            "position": index,
            "field": field,
            "value": value[:1024],
            "source": source,
            "missing": not bool(value),
        })
    return {
        "fields": fields,
        "values": values,
        "details": details,
        "missing_fields": sorted(set(missing)),
        "errors": mapping_errors,
        "valid": not mapping_errors and not missing,
    }


def render_positional_template(body_text: str, parameters: list[str]) -> str:
    """Render a {{1}}..{{N}} BODY preview without evaluating user-controlled markup."""
    body = str(body_text or "")
    for index, value in enumerate(parameters or [], start=1):
        body = re.sub(
            r"\{\{\s*" + str(index) + r"\s*\}\}",
            lambda _match, replacement=str(value): replacement,
            body,
        )
    return body


def preview_for_context(scope: str, body_text: str, mapping: Any, context: dict,
                       defaults: Any = None, *, expected_count: int | None = None,
                       goal_key: str | None = None) -> dict:
    resolved = resolve_parameters(
        scope, mapping, context, defaults,
        expected_count=expected_count, goal_key=goal_key,
    )
    return {
        **resolved,
        "body_text": str(body_text or ""),
        "message": render_positional_template(body_text, resolved["values"]),
    }


def missing_field_guidance(scope: str, fields: list[str]) -> list[str]:
    labels = {item["key"]: item["label"] for item in VARIABLES.get(str(scope or ""), ())}
    return [
        f"Completá «{labels.get(field, field)}» en la ficha del contacto o agregá un valor predeterminado en la campaña."
        for field in fields
    ]
