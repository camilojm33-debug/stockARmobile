"""WhatsApp Cloud API transport for the StockARmobile vendor agent."""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable

import requests

from .config_service import get_whatsapp_connection


class WhatsAppService:
    API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0")

    @classmethod
    def _post_message(cls, company, *, to: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        connection = get_whatsapp_connection(company)
        phone_number_id = connection["phone_number_id"]
        access_token = connection["access_token"]
        if not phone_number_id or not access_token:
            raise RuntimeError("WhatsApp no está configurado para esta empresa.")
        recipient = "".join(ch for ch in str(to or "") if ch.isdigit())
        if not recipient:
            raise ValueError("El número de WhatsApp del destinatario es inválido.")

        url = f"https://graph.facebook.com/{cls.API_VERSION}/{phone_number_id}/messages"
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=float(os.getenv("WHATSAPP_API_TIMEOUT", "20")),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"WhatsApp API HTTP {response.status_code}: {response.text[:500]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("WhatsApp API devolvió JSON inválido.") from exc
        return data

    @classmethod
    def send_text(cls, company, *, to: str, body: str) -> Dict[str, Any]:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "".join(ch for ch in str(to or "") if ch.isdigit()),
            "type": "text",
            "text": {"preview_url": True, "body": str(body or "")[:4096]},
        }
        return cls._post_message(company, to=to, payload=payload)

    @classmethod
    def send_template(
        cls,
        company,
        *,
        to: str,
        template_name: str,
        template_language: str = "es_AR",
        body_parameters: Iterable[str] | None = None,
    ) -> Dict[str, Any]:
        """Send an approved WhatsApp template with optional text body parameters."""
        name = str(template_name or "").strip()
        language = str(template_language or "es_AR").strip() or "es_AR"
        if not name:
            raise ValueError("La plantilla de WhatsApp es obligatoria.")

        template: Dict[str, Any] = {
            "name": name,
            "language": {"code": language},
        }
        values = [str(value or "")[:1024] for value in (body_parameters or [])]
        if values:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": value}
                        for value in values
                    ],
                }
            ]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "".join(ch for ch in str(to or "") if ch.isdigit()),
            "type": "template",
            "template": template,
        }
        return cls._post_message(company, to=to, payload=payload)
