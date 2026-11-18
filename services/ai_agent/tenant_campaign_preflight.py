"""Tenant CRM campaign preflight using the shared campaign template engine.

This adapter is intentionally tenant-scoped. It resolves only the active
company/branch WhatsApp connection and Client rows owned by that company.
It never reads SaaSLead prospect records or the SuperAdmin commercial WABA.
"""
from __future__ import annotations

from flask import current_app


def tenant_campaign_preflight(campaign, company) -> dict:
    from app import Client
    from services.ai_agent.tenant_campaign_service import (
        _audience_query,
        _campaign_branch_id,
        _client_phone,
        _json,
        _tenant_campaign_parameter_resolution,
        resolve_tenant_campaign_sender,
        _fetch_campaign_template,
    )
    from services.campaign_template_engine import (
        SCOPE_MERCHANT_CLIENTS,
        get_goal,
        goal_options,
        missing_field_guidance,
    )
    from sqlalchemy import func

    checks = []
    issues = []
    preview_message = ""
    preview_parameters = []
    preview_template = {}
    campaign_data = _json(campaign)

    def add(code, label, ok, detail, resolution=""):
        row = {
            "code": code,
            "label": label,
            "ok": bool(ok),
            "detail": str(detail or ""),
            "resolution": str(resolution or ""),
        }
        checks.append(row)
        if not ok:
            issues.append(row)

    goal_key = str(campaign_data.get("campaign_goal") or "customer_marketing")
    valid_goal_keys = {item["key"] for item in goal_options(SCOPE_MERCHANT_CLIENTS)}
    goal_ok = goal_key in valid_goal_keys
    goal = get_goal(
        SCOPE_MERCHANT_CLIENTS,
        goal_key if goal_ok else "customer_marketing",
    )
    goal_detail = (
        goal["label"]
        if goal_ok
        else f"El objetivo {goal_key!r} no existe para campañas de clientes."
    )
    add(
        "campaign_goal",
        "Objetivo de campaña",
        goal_ok,
        goal_detail,
        "Elegí un objetivo de clientes: promoción, reactivación, fidelización o novedades.",
    )
    add(
        "audience_scope",
        "Tipo de audiencia",
        campaign_data.get("audience_scope", SCOPE_MERCHANT_CLIENTS) == SCOPE_MERCHANT_CLIENTS,
        "La campaña se dirige exclusivamente a clientes de este comercio."
        if campaign_data.get("audience_scope", SCOPE_MERCHANT_CLIENTS) == SCOPE_MERCHANT_CLIENTS
        else "La campaña no está marcada como audiencia de clientes de este comercio.",
        "Las campañas de prospectos de StockARmobile pertenecen al CRM de SuperAdmin, no a este comercio.",
    )

    enabled = bool(current_app.config.get("AI_MARKETING_SEND_ENABLED", False))
    add(
        "marketing_engine",
        "Motor de Marketing IA",
        enabled,
        "El envío automático está habilitado." if enabled else "El envío automático de Marketing IA está deshabilitado.",
        "Habilitá AI_MARKETING_SEND_ENABLED para el worker y la aplicación.",
    )
    content_ok = bool(str(campaign.content or "").strip())
    add(
        "content",
        "Mensaje",
        content_ok,
        "El mensaje editable está completo." if content_ok else "El texto de campaña está vacío.",
        "Completá el mensaje editable para los clientes.",
    )

    sender = None
    connection = {}
    try:
        sender, connection = resolve_tenant_campaign_sender(
            company,
            _campaign_branch_id(campaign),
            require_ready=True,
        )
        connection_ok = bool(
            connection.get("enabled")
            and connection.get("phone_number_id")
            and connection.get("business_account_id")
            and connection.get("access_token")
        )
        sender_label = str(connection.get("display_phone_number") or "WhatsApp de esta sucursal")
        sender_detail = (
            f"Emisor: {sender_label}. WABA y número pertenecen a la sucursal activa."
            if connection_ok
            else "La conexión no tiene Phone Number ID, WABA o autorización de Meta."
        )
    except ValueError as exc:
        connection_ok = False
        sender_detail = str(exc)
    add(
        "whatsapp_sender",
        "WhatsApp del comercio",
        connection_ok,
        sender_detail,
        "Conectá o sincronizá el WhatsApp propio de esta sucursal desde Agentes IA → Vendedor IA.",
    )

    template = campaign_data.get("whatsapp_template") or {}
    template_ok = False
    template_error = ""
    if connection_ok:
        try:
            selected_name = str(template.get("name") or "").strip()
            selected_language = str(template.get("language") or "es_AR").strip()
            if selected_name:
                preview_template = _fetch_campaign_template(
                    connection,
                    selected_name=selected_name,
                    selected_language=selected_language,
                    allow_single_auto_select=False,
                )
            else:
                preview_template = _fetch_campaign_template(connection)
            goal_compatible = not preview_template.get("campaign_goal") or preview_template.get("campaign_goal") == goal_key
            template_ok = bool(
                preview_template.get("status") == "APPROVED"
                and preview_template.get("campaign_compatible")
                and goal_compatible
            )
            if not template_ok:
                template_error = (
                    "La plantilla maestra pertenece a otro objetivo de campaña."
                    if not goal_compatible
                    else str(preview_template.get("compatibility_reason") or "La plantilla no es compatible.")
                )
        except ValueError as exc:
            template_error = str(exc)
    else:
        template_error = "Primero debe estar lista la conexión WhatsApp de esta sucursal."
    add(
        "whatsapp_template",
        "Plantilla maestra / Meta",
        template_ok,
        (
            f"{preview_template.get('name')} · {preview_template.get('language')} · "
            f"{preview_template.get('category')} · APPROVED"
            if template_ok else template_error
        ),
        "Sincronizá las plantillas de esta WABA y elegí una aprobada de categoría MARKETING compatible con los tres parámetros del CRM.",
    )

    eligible_count = 0
    missing_count = 0
    missing_fields: dict[str, int] = {}
    recipient_total = 0
    if template_ok:
        expected = 3
        try:
            expected = int(
                len({
                    int(value)
                    for value in __import__("re").findall(
                        r"\{\{\s*(\d+)\s*\}\}",
                        str(preview_template.get("body_text") or ""),
                    )
                })
            )
        except Exception:
            expected = 3
        consented = _audience_query(campaign).filter(
            Client.whatsapp_marketing_consent == "opted_in",
            Client.whatsapp_marketing_consent_recorded_at.isnot(None),
            Client.whatsapp_marketing_consent_source.isnot(None),
            func.length(func.trim(Client.whatsapp_marketing_consent_source)) > 0,
            Client.whatsapp_marketing_consent_evidence.isnot(None),
            func.length(func.trim(Client.whatsapp_marketing_consent_evidence)) > 0,
        )
        for client in consented.yield_per(500):
            if not _client_phone(client):
                continue
            eligible_count += 1
            recipient_total += 1
            try:
                resolved = _tenant_campaign_parameter_resolution(
                    campaign,
                    client,
                    company,
                    preview_template,
                    campaign_data,
                )
            except ValueError:
                resolved = {"valid": False, "missing_fields": ["mensaje"], "errors": []}
            if not resolved.get("valid"):
                missing_count += 1
                for field in resolved.get("missing_fields") or []:
                    missing_fields[field] = missing_fields.get(field, 0) + 1
            elif not preview_message:
                preview_parameters = resolved.get("values") or []
                from services.campaign_template_engine import render_positional_template

                preview_message = render_positional_template(
                    preview_template.get("body_text") or "",
                    preview_parameters,
                )
        recipients_ok = eligible_count > 0 and missing_count == 0
        if eligible_count == 0:
            recipients_detail = "No hay clientes en esta audiencia con WhatsApp y consentimiento documentado."
        elif missing_count:
            fields_detail = ", ".join(
                f"{field} ({count})" for field, count in sorted(missing_fields.items())
            )
            recipients_detail = (
                f"{missing_count} de {eligible_count} cliente(s) tienen datos o parámetros faltantes: {fields_detail}."
            )
        else:
            recipients_detail = (
                f"{eligible_count} cliente(s) elegibles revisados; todos los parámetros están completos "
                "o tienen un valor predeterminado."
            )
        add(
            "recipient_validation",
            "Validación individual de destinatarios",
            recipients_ok,
            recipients_detail,
            "Completá los datos en la ficha del cliente o cargá un valor predeterminado para el campo indicado. Volvé a ejecutar el preflight.",
        )
        if missing_fields:
            add(
                "missing_fields",
                "Parámetros faltantes",
                False,
                "; ".join(missing_field_guidance(SCOPE_MERCHANT_CLIENTS, list(missing_fields))),
                "Editá el mapeo de variables o agregá valores predeterminados en el formulario de campaña.",
            )
        else:
            add("missing_fields", "Parámetros faltantes", True, "No se detectaron parámetros vacíos.")
    else:
        add(
            "recipient_validation",
            "Validación individual de destinatarios",
            False,
            "No se pueden validar parámetros hasta tener una plantilla Meta aprobada y compatible.",
            "Primero conectá WhatsApp y seleccioná una plantilla MARKETING aprobada.",
        )

    try:
        from app import Client as ClientModel
        audience_size = _audience_query(campaign).count()
        audience_text = (
            f"{eligible_count} con consentimiento documentado de {audience_size} cliente(s) que coinciden con los filtros."
        )
    except Exception:
        audience_text = "No se pudo calcular la audiencia."
    add(
        "audience",
        "Segmentación y consentimiento",
        eligible_count > 0 and missing_count == 0,
        audience_text,
        "Elegí el segmento correcto y registrá el consentimiento documentado en la ficha de cada cliente.",
    )

    return {
        "ready": not issues,
        "checks": checks,
        "issues": issues,
        "summary": "Lista para enviar." if not issues else f"{len(issues)} bloqueo(s) detectado(s).",
        "audience": {"eligible": eligible_count, "recipient_count": recipient_total},
        "preview_message": preview_message,
        "preview_template": preview_template,
        "preview_parameters": preview_parameters,
    }
