"""SuperAdmin-only discovery and compatibility checks for Meta campaign templates.

Unlike merchant-side templates, this module is scoped to the StockARmobile
commercial WABA. It supports positional BODY variables ({{1}}..{{N}}) while
failing closed for dynamic header, footer or button parameters.
"""
from __future__ import annotations

import re

from services.ai_agent.whatsapp_master_template_service import _list_waba_templates


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
MAX_BODY_VARIABLES = 10


def commercial_campaign_template_compatibility(template: dict) -> dict:
    """Describe whether an approved marketing template can be sent by CRM campaigns."""
    template = template if isinstance(template, dict) else {}
    status = str(template.get("status") or "").strip().upper()
    category = str(template.get("category") or "").strip().upper()
    components = template.get("components")
    components = components if isinstance(components, list) else []

    def result(ok: bool, reason: str, body_text: str = "", variable_count: int = 0) -> dict:
        return {
            "campaign_compatible": bool(ok),
            "compatibility_reason": str(reason or ""),
            "body_text": body_text,
            "variable_count": int(variable_count),
        }

    if status != "APPROVED":
        return result(False, f"Meta informa el estado {status or 'desconocido'}; se requiere APPROVED.")
    if category != "MARKETING":
        return result(False, f"La categoría de Meta es {category or 'desconocida'}; se requiere MARKETING.")

    body = next(
        (
            component for component in components
            if isinstance(component, dict)
            and str(component.get("type") or "").strip().upper() == "BODY"
        ),
        None,
    )
    body_text = str((body or {}).get("text") or "").strip()
    if not body_text:
        return result(False, "La plantilla no contiene un cuerpo de texto utilizable.")

    raw_tokens = _PLACEHOLDER_RE.findall(body_text)
    if any(not token.strip().isdigit() for token in raw_tokens):
        return result(False, "El cuerpo usa variables que no son posicionales ({{1}}, {{2}}, ...).", body_text)

    positions = sorted({int(token.strip()) for token in raw_tokens})
    if len(positions) > MAX_BODY_VARIABLES:
        return result(False, f"El cuerpo supera el máximo de {MAX_BODY_VARIABLES} variables admitidas.", body_text)
    if positions != list(range(1, len(positions) + 1)):
        return result(False, "Las variables del cuerpo deben ser consecutivas desde {{1}} y no dejar saltos.", body_text)

    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = str(component.get("type") or "").strip().upper()
        if component_type == "BODY":
            continue
        if component_type == "HEADER":
            header_format = str(component.get("format") or "").strip().upper()
            if header_format not in {"", "TEXT"}:
                return result(False, "La plantilla usa un encabezado multimedia que el CRM todavía no puede completar.", body_text, len(positions))
        if _PLACEHOLDER_RE.search(str(component)):
            return result(False, f"La plantilla contiene variables dinámicas fuera del cuerpo ({component_type or 'componente'}).", body_text, len(positions))

    return result(True, "Plantilla MARKETING aprobada y compatible con el envío del CRM.", body_text, len(positions))


def list_commercial_campaign_templates(connection: dict) -> list[dict]:
    """Fetch templates from the dedicated commercial WABA and annotate CRM compatibility."""
    raw_templates = _list_waba_templates(
        str((connection or {}).get("business_account_id") or "").strip(),
        str((connection or {}).get("access_token") or "").strip(),
    )
    result = []
    for item in raw_templates:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        language = str(item.get("language") or "").strip()
        if not name or not language:
            continue
        compatibility = commercial_campaign_template_compatibility(item)
        result.append({
            "name": name[:120],
            "language": language[:20],
            "status": str(item.get("status") or "UNKNOWN").strip().upper()[:30],
            "category": str(item.get("category") or "").strip().upper()[:40],
            **compatibility,
        })
    result.sort(key=lambda item: (
        not item["campaign_compatible"],
        item["name"].casefold(),
        item["language"].casefold(),
    ))
    return result
