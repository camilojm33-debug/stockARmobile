"""Preflight safety gates for SuperAdmin commercial campaigns."""
from __future__ import annotations
import re
import smtplib
from flask import current_app

_SUPPORTED_MERGE_FIELDS = {"empresa", "comercio", "contacto", "rubro", "localidad"}
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
    from services.saas_commercial_service import campaign_audience_metrics
    audience = campaign_audience_metrics(db_session, campaign)
    checks = []
    issues = []

    def add(code, label, ok, detail):
        row = {"code": code, "label": label, "ok": bool(ok), "detail": detail}
        checks.append(row)
        if not ok:
            issues.append(row)

    enabled = _enabled(current_app.config.get("SAAS_MARKETING_SEND_ENABLED"))
    add("marketing_engine", "Motor comercial", enabled, "Habilitado." if enabled else "Está deshabilitado.")

    identity_ok = bool(str(campaign.name or "").strip() and str(campaign.subject or "").strip())
    add("identity", "Identidad", identity_ok, "Nombre y asunto completos." if identity_ok else "Falta nombre o asunto.")

    content_ok = bool(str(campaign.body_html or "").strip())
    add("content", "Contenido", content_ok, "HTML presente." if content_ok else "El contenido HTML está vacío.")

    channel = str(campaign.channel or "").strip().lower()
    add("channel", "Canal", channel in {"email", "whatsapp", "both"}, f"Canal: {channel}.")

    placeholders = set(_MERGE_RE.findall("\n".join([
        str(campaign.subject or ""),
        str(campaign.body_text or ""),
        str(campaign.body_html or ""),
    ])))
    unsupported = sorted(placeholders - _SUPPORTED_MERGE_FIELDS)
    add("merge_fields", "Variables", not unsupported, "Variables soportadas." if not unsupported else f"No soportadas: {', '.join(unsupported)}.")

    target_count = int(campaign.target_count or 0)
    add("audience", "Destinatarios", target_count > 0, f"{target_count} preparados." if target_count > 0 else "No hay destinatarios preparados.")

    email_channel = channel in {"email", "both"}
    smtp_host = str(current_app.config.get("SMTP_HOST") or "").strip()
    smtp_user = str(current_app.config.get("SMTP_USER") or "").strip()
    smtp_password = current_app.config.get("SMTP_PASSWORD") or ""
    smtp_configured = bool(smtp_host and smtp_user and smtp_password)

    try:
        smtp_port = int(current_app.config.get("SMTP_PORT") or 587)
    except (TypeError, ValueError):
        smtp_port = 587

    smtp_ok = smtp_configured
    smtp_detail = "SMTP configurado."
    if email_channel and smtp_configured:
        smtp_ok, smtp_detail = _smtp_probe()
    email_eligible = int(audience.get("eligible_email") or 0)
    if email_channel:
        smtp_endpoint = f"{smtp_host or 'sin-host'}:{smtp_port}"
        smtp_detail = f"{smtp_detail} Servidor configurado: {smtp_endpoint}."
        if (not smtp_ok) and smtp_port == 587:
            smtp_detail += " Si este proveedor usa SSL directo, la alternativa habitual es puerto 465."
    add("email_config", "Email", (not email_channel) or smtp_ok, smtp_detail if email_channel else "No requerido.")
    add("email_audience", "Email elegible", (not email_channel) or email_eligible > 0, f"{email_eligible} con permiso." if email_eligible > 0 or not email_channel else "No hay emails con permiso registrado.")

    whatsapp_channel = channel in {"whatsapp", "both"}
    whatsapp_ok = True
    detail = "No requerido."
    if whatsapp_channel:
        try:
            from services.saas_commercial_whatsapp import commercial_whatsapp_summary
            connection = commercial_whatsapp_summary()
            template = str(campaign.whatsapp_template_name or connection.get("template_name") or "").strip()
            whatsapp_ok = bool(connection.get("connected") and template)
            display_number = str(connection.get("display_phone_number") or "").strip()
            sender_label = display_number or ("número comercial conectado" if connection.get("connected") else "sin conexión")
            detail = (
                f"Emisor: {sender_label}. Plantilla: {template}."
                if whatsapp_ok else "Falta conexión del número comercial o una plantilla aprobada configurada."
            )
        except Exception as exc:
            whatsapp_ok = False
            detail = f"No se pudo validar WhatsApp: {str(exc)[:250]}"
    add("whatsapp_config", "WhatsApp", whatsapp_ok, detail)

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
    )

    return {
        "ready": not issues,
        "checks": checks,
        "issues": issues,
        "audience": audience,
        "summary": "Lista para enviar." if not issues else f"{len(issues)} bloqueo(s) detectado(s).",
    }
