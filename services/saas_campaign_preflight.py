"""Preflight safety gates for SuperAdmin commercial campaigns."""
from __future__ import annotations
import re
import smtplib
from flask import current_app

_SUPPORTED_MERGE_FIELDS = {"empresa", "comercio", "contacto", "telefono", "email", "provincia", "rubro", "localidad", "producto", "asunto", "trial_url"}
_MERGE_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")

def _enabled(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _smtp_probe() -> tuple[bool, str]:
    from services.saas_commercial_service import _smtp_helo_name, _smtp_ports, _smtp_server

    host = str(current_app.config.get("SMTP_HOST") or "").strip()
    user = str(current_app.config.get("SMTP_USER") or "").strip()
    password = current_app.config.get("SMTP_PASSWORD") or ""
    try:
        preferred_port = int(current_app.config.get("SMTP_PORT") or 587)
    except (TypeError, ValueError):
        return False, "SMTP_PORT no es válido."

    if not host or not user or not password:
        return False, "Faltan credenciales SMTP."

    use_tls = bool(current_app.config.get("SMTP_USE_TLS", True))
    helo_name = _smtp_helo_name()
    failures = []

    for port in _smtp_ports(preferred_port):
        try:
            with _smtp_server(host, port, timeout=12, use_tls=use_tls, helo_name=helo_name) as server:
                features = dict(server.esmtp_features or {})
                advertised_auth = str(features.get("auth") or "").strip()
                if not advertised_auth:
                    failures.append(f"{port}: sin AUTH tras TLS; capacidades={','.join(sorted(features.keys())) or 'ninguna'}")
                    continue
                server.login(user, password)
            return True, f"SMTP verificado ({host}:{port}) por IPv4. AUTH={advertised_auth} HELO={helo_name}."
        except smtplib.SMTPAuthenticationError:
            return False, f"SMTP rechazó la autenticación en {host}:{port}. Revisá usuario, contraseña o App Password."
        except smtplib.SMTPServerDisconnected as exc:
            failures.append(f"{port}: el servidor SMTP cerró la conexión ({str(exc).strip() or 'sin detalle'})")
        except (OSError, TimeoutError) as exc:
            failures.append(f"{port}: {str(exc)[:160]}")
        except smtplib.SMTPException as exc:
            failures.append(f"{port}: SMTP {str(exc)[:160]}")
        except Exception as exc:
            failures.append(f"{port}: {str(exc)[:160]}")

    return False, f"No se pudo verificar SMTP en {host} usando HELO={helo_name}. " + "; ".join(failures)


def campaign_preflight(db_session, campaign) -> dict:
    from services.saas_commercial_service import (
        _commercial_campaign_settings,
        _commercial_lead_variable_context,
        _eligible_leads_query,
        _whatsapp_template_parameters,
        campaign_audience_metrics,
        get_commercial_whatsapp_parameter_fields,
        parse_segment_json,
    )
    from services.campaign_template_engine import (
        SCOPE_SAAS_PROSPECTS,
        goal_options,
        missing_field_guidance,
        normalize_mapping,
        render_positional_template,
        resolve_parameters,
    )

    audience = campaign_audience_metrics(db_session, campaign)
    checks = []
    issues = []
    preview_message = ""

    def add(code, label, ok, detail, resolution=""):
        row = {
            "code": code,
            "label": label,
            "ok": bool(ok),
            "detail": detail,
            "resolution": resolution,
        }
        checks.append(row)
        if not ok:
            issues.append(row)

    filters = parse_segment_json(campaign.segment_json)
    settings = _commercial_campaign_settings(campaign)
    stored_goal_key = str(filters.get("campaign_goal") or "saas_acquisition").strip()
    goal_key = settings.get("campaign_goal")
    valid_goals = {item["key"] for item in goal_options(SCOPE_SAAS_PROSPECTS)}
    goal_is_valid = stored_goal_key in valid_goals
    add(
        "campaign_goal",
        "Objetivo comercial",
        goal_is_valid,
        "Campaña de captación dirigida a prospectos de StockARmobile."
        if goal_is_valid else "El objetivo comercial no pertenece al catálogo de captación.",
        "Elegí un objetivo de captación o conversión de prospectos en el editor de campaña.",
    )
    scope_ok = filters.get("audience_scope", SCOPE_SAAS_PROSPECTS) == SCOPE_SAAS_PROSPECTS
    add(
        "audience_scope",
        "Tipo de audiencia",
        scope_ok,
        "Audiencia aislada de prospectos comerciales SaaSLead."
        if scope_ok else "La campaña no está configurada para prospectos comerciales.",
        "Las campañas de SuperAdmin solo pueden dirigirse a prospectos SaaS; los clientes finales se gestionan desde el CRM de cada comercio.",
    )

    enabled = _enabled(current_app.config.get("SAAS_MARKETING_SEND_ENABLED"))
    add("marketing_engine", "Motor comercial", enabled, "Habilitado." if enabled else "Está deshabilitado.", "Habilitá SAAS_MARKETING_SEND_ENABLED en el entorno del worker y de la aplicación.")

    identity_ok = bool(str(campaign.name or "").strip() and str(campaign.subject or "").strip())
    add("identity", "Identidad", identity_ok, "Nombre y asunto completos." if identity_ok else "Falta nombre o asunto.", "Completá el nombre interno y el asunto de la campaña.")

    content_ok = bool(str(campaign.body_html or "").strip())
    add("content", "Contenido", content_ok, "HTML presente." if content_ok else "El contenido HTML está vacío.", "Completá el cuerpo del email; WhatsApp usa el texto editable de la campaña.")

    channel = str(campaign.channel or "").strip().lower()
    add("channel", "Canal", channel in {"email", "whatsapp", "both"}, f"Canal: {channel}.", "Elegí Email, WhatsApp o ambos canales.")

    placeholders = set(_MERGE_RE.findall("\n".join([
        str(campaign.subject or ""),
        str(campaign.body_text or ""),
        str(campaign.body_html or ""),
    ])))
    unsupported = sorted(placeholders - _SUPPORTED_MERGE_FIELDS)
    add(
        "merge_fields",
        "Variables de contenido",
        not unsupported,
        "Variables soportadas." if not unsupported else f"No soportadas: {', '.join(unsupported)}.",
        "Reemplazá las variables desconocidas por campos del catálogo de contactos.",
    )

    target_count = int(campaign.target_count or 0)
    add("audience", "Destinatarios preparados", target_count > 0, f"{target_count} preparado(s)." if target_count > 0 else "No hay destinatarios preparados.", "Revisá los filtros, el consentimiento y prepará nuevamente la audiencia.")

    email_channel = channel in {"email", "both"}
    smtp_configured = bool(
        current_app.config.get("SMTP_HOST")
        and current_app.config.get("SMTP_USER")
        and current_app.config.get("SMTP_PASSWORD")
    )
    gmail_api_configured = False
    gmail_api_ok = False
    gmail_api_detail = "Gmail API no está configurado."
    try:
        from services.gmail_commercial_service import (
            commercial_gmail_configuration_status,
            commercial_gmail_send_status,
        )
        gmail_api_configured = bool(commercial_gmail_configuration_status().get("send_configured"))
    except Exception:
        gmail_api_configured = False

    email_ok = False
    email_detail = "No hay un proveedor de email listo."
    if email_channel and gmail_api_configured:
        gmail_api_ok, gmail_api_detail = commercial_gmail_send_status()
        email_ok = gmail_api_ok
        email_detail = gmail_api_detail
    elif email_channel and smtp_configured:
        email_ok, email_detail = _smtp_probe()
    email_eligible = int(audience.get("eligible_email") or 0)
    add("email_config", "Proveedor de email", (not email_channel) or email_ok, email_detail if email_channel else "No requerido.", "Revisá Gmail API o las credenciales SMTP." if email_channel else "")
    add("email_audience", "Emails elegibles", (not email_channel) or email_eligible > 0, f"{email_eligible} con consentimiento." if email_eligible > 0 or not email_channel else "No hay emails con permiso registrado.", "Registrá consentimiento verificable o modificá la segmentación.")

    whatsapp_channel = channel in {"whatsapp", "both"}
    whatsapp_ok = True
    template_validation = {}
    fields = get_commercial_whatsapp_parameter_fields(campaign)
    expected_parameters = 0
    if whatsapp_channel:
        try:
            from services.saas_commercial_whatsapp import (
                commercial_whatsapp_summary,
                validate_commercial_whatsapp_template,
            )
            connection = commercial_whatsapp_summary()
            template_name = str(campaign.whatsapp_template_name or connection.get("template_name") or "").strip()
            display_number = str(connection.get("display_phone_number") or "").strip()
            sender_label = display_number or ("número comercial conectado" if connection.get("connected") else "sin conexión")
            template_validation = validate_commercial_whatsapp_template(
                template_name,
                campaign.whatsapp_template_language or "es_AR",
            )
            expected_parameters = int(template_validation.get("variable_count") or 0)
            fields, mapping_errors = normalize_mapping(
                SCOPE_SAAS_PROSPECTS,
                fields,
                expected_count=expected_parameters,
                goal_key=goal_key,
            )
            mapping_ok = bool(template_validation.get("ok") and not mapping_errors)
            whatsapp_ok = bool(connection.get("connected") and mapping_ok)
            mapping_detail = "Mapeo válido." if not mapping_errors else " ".join(mapping_errors)
            add(
                "whatsapp_config",
                "WhatsApp Comercial y plantilla Meta",
                bool(connection.get("connected") and template_validation.get("ok") and mapping_ok),
                f"Emisor: {sender_label}. Plantilla: {template_name or 'sin configurar'}. "
                f"{template_validation.get('detail') or 'No validada.'} {mapping_detail}",
                "Conectá la WABA comercial de SuperAdmin y seleccioná una plantilla APPROVED de categoría MARKETING.",
            )
            add(
                "whatsapp_mapping",
                "Parámetros de plantilla",
                not mapping_errors,
                "Cada variable posicional tiene un campo asignado."
                if not mapping_errors else mapping_detail,
                "En el editor, asigná un campo y un valor predeterminado a cada parámetro que no exista en todos los prospectos.",
            )
        except Exception as exc:
            whatsapp_ok = False
            template_validation = {}
            mapping_errors = [str(exc)]
            mapping_detail = str(exc)[:240]
            add(
                "whatsapp_config",
                "WhatsApp Comercial y plantilla Meta",
                False,
                f"No se pudo validar WhatsApp con Meta: {str(exc)[:240]}",
                "Verificá la conexión de la WABA comercial, permisos y plantilla aprobada.",
            )
            add(
                "whatsapp_mapping",
                "Parámetros de plantilla",
                False,
                mapping_detail,
                "Actualizá las plantillas y completá el mapeo de parámetros.",
            )
    else:
        add("whatsapp_config", "WhatsApp Comercial y plantilla Meta", True, "No requerido.")
        add("whatsapp_mapping", "Parámetros de plantilla", True, "No requerido.")

    recipient_validation_ok = True
    recipient_detail = "No requerido."
    if whatsapp_channel and template_validation.get("ok") and not mapping_errors:
        from services.campaign_template_engine import parse_defaults
        defaults = parse_defaults(SCOPE_SAAS_PROSPECTS, settings.get("parameter_defaults"))
        query = _eligible_leads_query("whatsapp", filters).order_by("lead_score", "id")
        sample_leads = query.yield_per(500)
        missing_by_field = {}
        invalid_count = 0
        total_leads = 0
        for lead in sample_leads:
            total_leads += 1
            try:
                resolved = resolve_parameters(
                    SCOPE_SAAS_PROSPECTS,
                    fields,
                    _commercial_lead_variable_context(campaign, lead),
                    defaults,
                    expected_count=expected_parameters,
                    goal_key=goal_key,
                )
                if not resolved["valid"]:
                    invalid_count += 1
                    for field in resolved["missing_fields"]:
                        missing_by_field[field] = missing_by_field.get(field, 0) + 1
                elif not preview_message:
                    preview_message = render_positional_template(
                        str(template_validation.get("body_text") or ""),
                        resolved["values"],
                    )
            except Exception:
                invalid_count += 1
        recipient_validation_ok = total_leads > 0 and invalid_count == 0
        if total_leads == 0:
            recipient_detail = "No hay prospectos con WhatsApp válido y consentimiento vigente para esta segmentación."
        elif invalid_count:
            detail = ", ".join(f"{field}: {count}" for field, count in sorted(missing_by_field.items()))
            recipient_detail = f"{invalid_count} de {total_leads} prospecto(s) tienen parámetros sin resolver. {detail}."
        else:
            recipient_detail = f"{total_leads} prospecto(s) revisados; todos los parámetros tienen datos o valores predeterminados."
        add(
            "recipient_data",
            "Datos por destinatario",
            recipient_validation_ok,
            recipient_detail,
            "Completá los datos faltantes en la ficha del prospecto o cargá valores predeterminados en la campaña. No se enviarán parámetros vacíos.",
        )
    else:
        add(
            "recipient_data",
            "Datos por destinatario",
            not whatsapp_channel,
            "No se pudo revisar la resolución de parámetros porque la plantilla o su mapeo no está validado."
            if whatsapp_channel else "No requerido.",
            "Primero validá la plantilla y el mapeo de sus variables.",
        )

    email_preview_subject = ""
    email_preview_text = ""
    if email_channel:
        from services.saas_commercial_service import (
            _campaign_email_is_valid,
            _commercial_email_missing_fields,
            _eligible_leads_query,
            render_merge,
        )
        email_total = 0
        email_invalid = 0
        missing_email_fields: dict[str, int] = {}
        email_leads = _eligible_leads_query("email", filters).order_by("lead_score", "id")
        for lead in email_leads.yield_per(500):
            if not _campaign_email_is_valid(lead):
                continue
            email_total += 1
            try:
                missing = _commercial_email_missing_fields(campaign, lead)
            except ValueError as exc:
                missing = [str(exc)]
            if missing:
                email_invalid += 1
                for field in missing:
                    missing_email_fields[field] = missing_email_fields.get(field, 0) + 1
            elif not email_preview_subject:
                email_preview_subject = render_merge(campaign.subject, lead, campaign=campaign)
                email_preview_text = render_merge(
                    campaign.body_text or campaign.body_html,
                    lead,
                    campaign=campaign,
                )
        email_recipient_ok = email_total > 0 and email_invalid == 0
        if email_total == 0:
            email_recipient_detail = "No hay prospectos con email válido y consentimiento vigente para esta segmentación."
        elif email_invalid:
            missing_summary = ", ".join(
                f"{field}: {count}" for field, count in sorted(missing_email_fields.items())
            )
            email_recipient_detail = (
                f"{email_invalid} de {email_total} prospecto(s) tienen variables de email faltantes: {missing_summary}."
            )
        else:
            email_recipient_detail = (
                f"{email_total} prospecto(s) revisados; todos los campos del email están completos "
                "o tienen valores predeterminados."
            )
        add(
            "email_recipient_data",
            "Datos personalizados de email",
            email_recipient_ok,
            email_recipient_detail,
            "Completá los campos que faltan en la ficha del prospecto o agregá un valor predeterminado en la campaña.",
        )

    try:
        from services.saas_commercial_service import crm_campaign_dispatch_worker_started
        worker_started = crm_campaign_dispatch_worker_started()
    except Exception:
        worker_started = False
    add(
        "worker",
        "Worker comercial",
        worker_started,
        "Dispatcher interno iniciado." if worker_started else "El dispatcher interno de campañas no está iniciado en este proceso.",
        "Verificá el proceso de dispatch de campañas comerciales.",
    )

    return {
        "ready": not issues,
        "checks": checks,
        "issues": issues,
        "audience": audience,
        "preview_message": preview_message,
        "preview_email_subject": email_preview_subject,
        "preview_email_text": email_preview_text,
        "template_validation": template_validation,
        "summary": "Lista para enviar." if not issues else f"{len(issues)} bloqueo(s) detectado(s).",
    }
