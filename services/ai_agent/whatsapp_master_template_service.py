"""Provision StockARmobile master WhatsApp templates into each merchant WABA.

Meta keeps templates inside each WhatsApp Business Account. This module stores
StockARmobile's reusable template definitions and idempotently creates a copy
inside the connected merchant WABA. Meta approval remains account-specific.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests


MASTER_TEMPLATES = (
    {
        "key": "marketing_general",
        "name": "campana_marketing_es_ar",
        "display_name": "Campaña de marketing general",
        "language": "es_AR",
        "language_label": "Español (Argentina)",
        "category": "MARKETING",
        "description": "Plantilla predeterminada para promociones, productos y novedades del comercio.",
        "body_text": (
            "¡Hola, {{1}}! 👋\n\n"
            "Queremos compartirte novedades de nuestro comercio.\n\n"
            "Producto o novedad: {{2}}.\n\n"
            "{{3}}\n\n"
            "Para dejar de recibir mensajes promocionales, respondé BAJA.\n\n"
            "¡Gracias por elegirnos!"
        ),
        "examples": [
            "María",
            "Productos seleccionados",
            "Esta semana tenemos novedades y beneficios especiales. ¡Vení a conocerlos!",
        ],
    },,
    {
        "key": "customer_reactivation",
        "goal_key": "customer_reactivation",
        "name": "campana_reactivacion_es_ar",
        "display_name": "Recuperación de clientes inactivos",
        "language": "es_AR",
        "language_label": "Español (Argentina)",
        "category": "MARKETING",
        "description": "Invita a volver a clientes que hace tiempo no realizan compras.",
        "body_text": (
            "¡Hola, {{1}}! 👋\n\n"
            "Hace un tiempo que no te vemos por {{2}}.\n\n"
            "{{3}}\n\n"
            "Para dejar de recibir mensajes promocionales, respondé BAJA.\n\n"
            "¡Nos encantaría volver a verte!"
        ),
        "examples": ["María", "nuestro comercio", "Tenemos una propuesta especial para tu próxima visita."],
    },
    {
        "key": "customer_loyalty",
        "goal_key": "customer_loyalty",
        "name": "campana_fidelizacion_es_ar",
        "display_name": "Fidelización de clientes",
        "language": "es_AR",
        "language_label": "Español (Argentina)",
        "category": "MARKETING",
        "description": "Agradece la preferencia y comunica beneficios a clientes habituales.",
        "body_text": (
            "¡Hola, {{1}}! 💚\n\n"
            "Gracias por elegir {{2}} y acompañarnos.\n\n"
            "{{3}}\n\n"
            "Para dejar de recibir mensajes promocionales, respondé BAJA.\n\n"
            "¡Gracias por tu confianza!"
        ),
        "examples": ["María", "nuestro comercio", "Tenemos beneficios especiales para agradecer tu preferencia."],
    },
    {
        "key": "customer_new_product",
        "goal_key": "customer_new_product",
        "name": "campana_novedad_producto_es_ar",
        "display_name": "Nuevos productos y novedades",
        "language": "es_AR",
        "language_label": "Español (Argentina)",
        "category": "MARKETING",
        "description": "Presenta productos nuevos, lanzamientos y novedades del comercio.",
        "body_text": (
            "¡Hola, {{1}}! ✨\n\n"
            "Tenemos una novedad en {{2}}.\n\n"
            "{{3}}\n\n"
            "Para dejar de recibir mensajes promocionales, respondé BAJA.\n\n"
            "¡Te esperamos!"
        ),
        "examples": ["María", "nuestro comercio", "Conocé los nuevos productos que llegaron esta semana."],
    }
)

MAX_TEMPLATE_PAGES = 20
REQUEST_TIMEOUT_SECONDS = 10
_META_TEMPLATE_FIELDS = "id,name,status,category,language,components"


def list_master_templates() -> list[dict]:
    """Return safe presentation data for the SuperAdmin master-template screen."""
    return [
        {
            "key": item["key"],
            "name": item["name"],
            "display_name": item["display_name"],
            "language": item["language"],
            "language_label": item["language_label"],
            "category": item["category"],
            "description": item["description"],
            "body_text": item["body_text"],
            "goal_key": str(item.get("goal_key") or item["key"]),
            "default_parameter_fields": ["contacto", "producto", "mensaje"],
        }
        for item in MASTER_TEMPLATES
    ]


def record_master_template_provisioning(company, results: list[dict], *, branch=None) -> dict:
    """Persist the latest non-secret provisioning state for the SuperAdmin overview."""
    safe_results = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        safe_results.append({
            "key": str(item.get("key") or "")[:80],
            "name": str(item.get("name") or "")[:120],
            "language": str(item.get("language") or "")[:20],
            "status": str(item.get("status") or "UNKNOWN").upper()[:30],
            "created": bool(item.get("created")),
            "message": str(item.get("message") or "")[:300],
        })
    state = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "templates": safe_results,
    }

    if branch is not None and not bool(getattr(branch, "is_default", False)):
        from services.ai_agent.config_service import _branch_preferences, save_branch_preferences

        prefs = _branch_preferences(branch)
        whatsapp = prefs.get("whatsapp") if isinstance(prefs.get("whatsapp"), dict) else {}
        whatsapp["master_template_sync"] = state
        prefs["whatsapp"] = whatsapp
        save_branch_preferences(branch, prefs)
    else:
        from services.ai_agent.config_service import update_ai_preferences

        update_ai_preferences(company, whatsapp_updates={"master_template_sync": state})
    return state


def _graph_version() -> str:
    version = (
        os.getenv("META_GRAPH_API_VERSION")
        or os.getenv("WHATSAPP_GRAPH_API_VERSION")
        or "v26.0"
    ).strip()
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("La versión configurada de Meta Graph API no es válida.")
    return version


def _graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{_graph_version()}/{str(path).lstrip('/')}"


def _response_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return f"Meta respondió HTTP {response.status_code}."
    code = error.get("code")
    detail = str(
        error.get("error_user_msg")
        or error.get("message")
        or error.get("type")
        or ""
    ).strip()
    if code == 200 or "permission" in detail.lower():
        return (
            "Meta no autorizó crear la plantilla en esta cuenta. La autorización "
            "de WhatsApp debe incluir el permiso whatsapp_business_management. "
            "Volvé a conectar WhatsApp desde StockARmobile y verificá los permisos de Meta."
        )
    detail = re.sub(r"\s+", " ", detail)[:220]
    return f"Meta respondió HTTP {response.status_code}" + (f": {detail}" if detail else ".")


def _normalise_body(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _template_body(template: dict) -> str:
    components = template.get("components")
    if not isinstance(components, list):
        return ""
    for component in components:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "BODY":
            return str(component.get("text") or "")
    return ""


def _list_waba_templates(waba_id: str, access_token: str) -> list[dict]:
    url = _graph_url(f"{waba_id}/message_templates")
    params = {"fields": _META_TEMPLATE_FIELDS, "limit": 100}
    headers = {"Authorization": f"Bearer {access_token}"}
    templates: list[dict] = []
    visited: set[str] = set()

    try:
        for _ in range(MAX_TEMPLATE_PAGES):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "graph.facebook.com":
                raise ValueError("Meta devolvió una dirección de paginación no válida.")
            if url in visited:
                raise ValueError("Meta devolvió una página repetida al consultar las plantillas.")
            visited.add(url)

            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if not response.ok:
                raise ValueError(_response_error(response))
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Meta devolvió una respuesta inválida al consultar las plantillas.") from exc
            if not isinstance(payload, dict):
                raise ValueError("Meta devolvió una lista de plantillas con formato inválido.")
            data = payload.get("data") or []
            if isinstance(data, list):
                templates.extend(item for item in data if isinstance(item, dict))

            next_url = str((payload.get("paging") or {}).get("next") or "").strip()
            if not next_url:
                return templates
            url, params = next_url, None
    except requests.Timeout as exc:
        raise ValueError("Meta tardó demasiado en responder al consultar las plantillas.") from exc
    except requests.ConnectionError as exc:
        raise ValueError("No se pudo conectar con Meta para consultar las plantillas.") from exc
    except requests.RequestException as exc:
        raise ValueError("Falló la consulta de plantillas de WhatsApp con Meta.") from exc

    raise ValueError(
        f"Meta devolvió más de {MAX_TEMPLATE_PAGES} páginas de plantillas. "
        "Contactá a soporte para revisar esta cuenta."
    )


def _existing_template_result(master: dict, existing: dict) -> dict:
    name = str(existing.get("name") or "")
    language = str(existing.get("language") or "")
    category = str(existing.get("category") or "").upper()
    status = str(existing.get("status") or "UNKNOWN").upper()
    body_matches = _normalise_body(_template_body(existing)) == _normalise_body(master["body_text"])
    category_matches = category == master["category"]

    if not body_matches or not category_matches:
        return {
            "key": master["key"],
            "name": name,
            "language": language,
            "status": "CONFLICT",
            "created": False,
            "message": (
                "Ya existe una plantilla con este nombre, pero su contenido o categoría "
                "no coincide con la plantilla maestra. No se modificó ni se reemplazó."
            ),
        }

    if status == "APPROVED":
        message = "La plantilla maestra ya está aprobada y lista para campañas."
    elif status in {"PENDING", "IN_APPEAL"}:
        message = "La plantilla maestra ya fue enviada a Meta y está pendiente de aprobación."
    elif status == "REJECTED":
        message = "Meta rechazó esta plantilla. Revisá el motivo en WhatsApp Manager."
    else:
        message = f"Meta informa el estado {status}. Revisalo en WhatsApp Manager."

    return {
        "key": master["key"],
        "name": name,
        "language": language,
        "status": status,
        "created": False,
        "message": message,
    }


def provision_master_templates(connection: dict) -> list[dict]:
    """Create missing master templates in the connected WABA without touching existing ones.

    The operation is safe to retry: an existing matching template is reused;
    a name collision with different content is reported and never overwritten.
    """
    connection = connection if isinstance(connection, dict) else {}
    if not connection.get("enabled"):
        raise ValueError("El WhatsApp de este comercio está deshabilitado.")
    if not str(connection.get("phone_number_id") or "").strip():
        raise ValueError("Falta el Phone Number ID del WhatsApp conectado.")
    waba_id = str(connection.get("business_account_id") or "").strip()
    access_token = str(connection.get("access_token") or "").strip()
    if not re.fullmatch(r"\d{1,30}", waba_id):
        raise ValueError("Falta un WABA ID válido para el WhatsApp conectado.")
    if not access_token:
        raise ValueError("Falta la autorización de Meta para administrar las plantillas de esta cuenta.")

    existing_templates = _list_waba_templates(waba_id, access_token)
    results: list[dict] = []

    for master in MASTER_TEMPLATES:
        existing = next(
            (
                item for item in existing_templates
                if str(item.get("name") or "").strip() == master["name"]
                and str(item.get("language") or "").strip() == master["language"]
            ),
            None,
        )
        if existing is not None:
            results.append(_existing_template_result(master, existing))
            continue

        body_component = {
            "type": "BODY",
            "text": master["body_text"],
            "example": {"body_text": [list(master["examples"])]},
        }
        payload = {
            "name": master["name"],
            "language": master["language"],
            "category": master["category"],
            "components": [body_component],
        }
        try:
            response = requests.post(
                _graph_url(f"{waba_id}/message_templates"),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if not response.ok:
                # A concurrent request or a previous timed-out request may have
                # created it after the initial read. Re-read once before reporting failure.
                try:
                    refreshed = _list_waba_templates(waba_id, access_token)
                    raced = next(
                        (
                            item for item in refreshed
                            if str(item.get("name") or "").strip() == master["name"]
                            and str(item.get("language") or "").strip() == master["language"]
                        ),
                        None,
                    )
                except ValueError:
                    raced = None
                if raced is not None:
                    results.append(_existing_template_result(master, raced))
                    continue
                results.append({
                    "key": master["key"],
                    "name": master["name"],
                    "language": master["language"],
                    "status": "ERROR",
                    "created": False,
                    "message": _response_error(response),
                })
                continue

            try:
                created_payload = response.json()
            except ValueError:
                created_payload = {}
            if not isinstance(created_payload, dict):
                created_payload = {}
            status = str(created_payload.get("status") or "PENDING").upper()
            results.append({
                "key": master["key"],
                "name": master["name"],
                "language": master["language"],
                "status": status,
                "created": True,
                "template_id": str(created_payload.get("id") or ""),
                "message": (
                    "Plantilla creada y enviada a revisión de Meta. "
                    "Se habilitará para campañas cuando Meta la apruebe."
                ),
            })
        except requests.Timeout:
            results.append({
                "key": master["key"],
                "name": master["name"],
                "language": master["language"],
                "status": "UNCERTAIN",
                "created": False,
                "message": (
                    "Meta no respondió a tiempo. No repitas la creación manualmente; "
                    "presioná Sincronizar para comprobar si Meta recibió la solicitud."
                ),
            })
        except requests.ConnectionError:
            results.append({
                "key": master["key"],
                "name": master["name"],
                "language": master["language"],
                "status": "ERROR",
                "created": False,
                "message": "No se pudo conectar con Meta para crear la plantilla. Intentá sincronizar nuevamente.",
            })
        except requests.RequestException:
            results.append({
                "key": master["key"],
                "name": master["name"],
                "language": master["language"],
                "status": "ERROR",
                "created": False,
                "message": "Falló la solicitud de creación de la plantilla en Meta.",
            })

    return results
