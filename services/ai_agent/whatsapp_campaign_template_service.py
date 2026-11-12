"""Validation and discovery for the shared StockARmobile WhatsApp campaign template."""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

import requests


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_REQUIRED_BODY_VARIABLES = {"1", "2", "3"}
MAX_TEMPLATE_PAGES = 20


def _meta_error_message(response: requests.Response) -> str:
    """Return a short provider error without exposing request headers or tokens."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        detail = str(
            error.get("error_user_msg")
            or error.get("message")
            or error.get("type")
            or ""
        ).strip()
    else:
        detail = ""
    detail = re.sub(r"\s+", " ", detail)[:240]
    return f"Meta respondió HTTP {response.status_code}" + (f": {detail}" if detail else ".")


def campaign_template_compatibility(template: dict) -> tuple[bool, str, str]:
    """Validate that a Meta template can carry the CRM's editable marketing copy.

    Campaign delivery currently sends three BODY parameters only. Media headers,
    dynamic headers/footers/buttons and non-marketing categories must therefore
    not be presented as compatible.
    """
    if str(template.get("status") or "").upper() != "APPROVED":
        return False, "La plantilla no está aprobada por Meta.", ""

    category = str(template.get("category") or "").strip().upper()
    if category != "MARKETING":
        return False, "Para campañas promocionales se necesita una plantilla de categoría MARKETING.", ""

    components = template.get("components")
    if not isinstance(components, list):
        components = []

    body = next(
        (
            component for component in components
            if isinstance(component, dict)
            and str(component.get("type") or "").upper() == "BODY"
        ),
        None,
    )
    body_text = str((body or {}).get("text") or "").strip()
    body_tokens = _PLACEHOLDER_RE.findall(body_text)
    body_variables = {token.strip() for token in body_tokens}
    if not body_text or body_variables != _REQUIRED_BODY_VARIABLES:
        return (
            False,
            "El cuerpo debe incluir {{1}} (cliente), {{2}} (producto) y {{3}} (mensaje), sin variables adicionales.",
            body_text,
        )

    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = str(component.get("type") or "").upper()
        if component_type == "BODY":
            continue
        if component_type == "HEADER":
            header_format = str(component.get("format") or "").upper()
            if header_format not in {"", "TEXT"}:
                return False, "La plantilla usa un encabezado multimedia que todavía no admite el envío de campañas.", body_text
            if _PLACEHOLDER_RE.search(str(component.get("text") or "")):
                return False, "El encabezado tiene variables dinámicas no admitidas por el envío de campañas.", body_text
        elif _PLACEHOLDER_RE.search(json.dumps(component, ensure_ascii=False)):
            return False, "La plantilla contiene variables fuera del cuerpo que el envío no puede completar.", body_text

    return True, "", body_text


def list_approved_campaign_templates(connection: dict) -> list[dict]:
    """List approved WABA templates and explain which ones support CRM campaigns."""
    waba_id = str(connection.get("business_account_id") or "").strip()
    access_token = str(connection.get("access_token") or "").strip()
    if not waba_id:
        raise ValueError("Falta el WABA ID del número comercial de StockARmobile.")
    if not access_token:
        raise ValueError("Falta el token de acceso de Meta para el número comercial. Volvé a autorizar WhatsApp.")

    version = (
        os.getenv("META_GRAPH_API_VERSION")
        or os.getenv("WHATSAPP_GRAPH_API_VERSION")
        or "v26.0"
    ).strip()
    url = f"https://graph.facebook.com/{version}/{waba_id}/message_templates"
    params = {"fields": "name,status,category,language,components", "limit": 100}
    headers = {"Authorization": f"Bearer {access_token}"}
    templates: list[dict] = []
    visited_urls: set[str] = set()

    try:
        for _page in range(MAX_TEMPLATE_PAGES):
            parsed_url = urlparse(url)
            if parsed_url.hostname != "graph.facebook.com":
                raise ValueError("Meta devolvió una dirección de paginación no válida.")
            if url in visited_urls:
                raise ValueError("Meta devolvió una paginación repetida al consultar las plantillas.")
            visited_urls.add(url)

            response = requests.get(url, params=params, headers=headers, timeout=20)
            if not response.ok:
                raise ValueError(_meta_error_message(response))
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Meta devolvió una respuesta inválida al consultar las plantillas.") from exc
            if not isinstance(payload, dict):
                raise ValueError("Meta devolvió una lista de plantillas con formato inválido.")

            for item in payload.get("data") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                language = str(item.get("language") or "").strip()
                status = str(item.get("status") or "").strip().upper()
                if not name or not language or status != "APPROVED":
                    continue
                compatible, reason, body_text = campaign_template_compatibility(item)
                templates.append({
                    "name": name[:120],
                    "language": language[:20],
                    "category": str(item.get("category") or "").strip().upper()[:40],
                    "status": status,
                    "body_text": body_text,
                    "campaign_compatible": compatible,
                    "compatibility_reason": reason,
                })

            next_url = str((payload.get("paging") or {}).get("next") or "").strip()
            if not next_url:
                break
            url, params = next_url, None
        else:
            raise ValueError(
                f"Meta devolvió más de {MAX_TEMPLATE_PAGES} páginas de plantillas. "
                "Reducí la cantidad de plantillas de la WABA o contactá a soporte."
            )
    except requests.Timeout as exc:
        raise ValueError("Meta tardó demasiado en responder al consultar las plantillas de WhatsApp.") from exc
    except requests.ConnectionError as exc:
        raise ValueError("No se pudo conectar con Meta para consultar las plantillas de WhatsApp.") from exc
    except requests.RequestException as exc:
        raise ValueError("Falló la consulta de plantillas de WhatsApp con Meta.") from exc

    return templates


def get_campaign_template(
    connection: dict,
    *,
    selected_name: str | None = None,
    selected_language: str | None = None,
    allow_single_auto_select: bool = True,
) -> dict:
    """Resolve the configured template, or safely use one unique compatible template."""
    if not connection.get("enabled"):
        raise ValueError("El número oficial de WhatsApp de StockARmobile está deshabilitado.")
    if not str(connection.get("phone_number_id") or "").strip():
        raise ValueError("Falta el Phone Number ID del número oficial de StockARmobile.")
    if not str(connection.get("business_account_id") or "").strip():
        raise ValueError("Falta el WABA ID del número oficial de StockARmobile.")
    if not str(connection.get("access_token") or "").strip():
        raise ValueError("Falta el token de Meta del número oficial. Un SuperAdmin debe renovar la conexión.")

    name = str(selected_name or connection.get("template_name") or "").strip()
    language = str(selected_language or connection.get("template_language") or "es_AR").strip() or "es_AR"
    templates = list_approved_campaign_templates(connection)

    if name:
        selected = next(
            (
                item for item in templates
                if item["name"] == name and item["language"] == language
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                f"La plantilla configurada «{name}» ({language}) no figura aprobada en la WABA comercial. "
                "Sincronizá las plantillas desde SuperAdmin → WhatsApp Comercial y seleccioná otra aprobada."
            )
        if not selected["campaign_compatible"]:
            raise ValueError(
                f"La plantilla «{name}» está aprobada, pero no sirve para campañas: "
                f"{selected['compatibility_reason']}"
            )
        return {**selected, "auto_selected": False}

    compatible = [item for item in templates if item["campaign_compatible"]]
    if allow_single_auto_select and len(compatible) == 1:
        return {**compatible[0], "auto_selected": True}

    if not compatible:
        rejected = next(
            (item["compatibility_reason"] for item in templates if item.get("compatibility_reason")),
            "",
        )
        detail = f" Motivo: {rejected}" if rejected else ""
        raise ValueError(
            "No hay una plantilla aprobada compatible en la WABA del número oficial de StockARmobile. "
            "Un SuperAdmin debe aprobar una plantilla MARKETING con {{1}} cliente, {{2}} producto "
            "y {{3}} mensaje editable desde Meta." + detail
        )

    raise ValueError(
        f"Hay {len(compatible)} plantillas aprobadas compatibles y todavía no se eligió una predeterminada. "
        "Un SuperAdmin debe seleccionarla en SuperAdmin → WhatsApp Comercial → Plantillas aprobadas."
    )
