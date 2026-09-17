"""Stable public publication layer for each merchant's Vendor IA."""
from __future__ import annotations

import base64
import io
import secrets
from datetime import datetime, timezone

import qrcode
from flask import jsonify, render_template, request, url_for, abort, current_app
from flask.signals import appcontext_pushed
from flask_login import current_user

from stockarmobile.decorators import company_admin_required
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation
from services.ai_agent.config_service import get_ai_preferences, update_ai_preferences, ensure_default_agents, VENDOR_AGENT_NAME
from services.ai_agent.usage_service import can_use_ai


PUBLIC_VENDOR_AI_KEY = "public_vendor"
PUBLIC_VENDOR_SLUG_SIZE = 12


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_slug() -> str:
    return secrets.token_urlsafe(PUBLIC_VENDOR_SLUG_SIZE).replace("_", "-").replace("/", "-")


def _publication_from_company(company) -> dict:
    ai = get_ai_preferences(company).get("ai_agent", {})
    raw = ai.get(PUBLIC_VENDOR_AI_KEY) if isinstance(ai.get(PUBLIC_VENDOR_AI_KEY), dict) else {}
    return {
        "slug": str(raw.get("slug") or "").strip(),
        "published": bool(raw.get("published", False)),
        "published_at": str(raw.get("published_at") or "").strip(),
        "rotated_at": str(raw.get("rotated_at") or "").strip(),
    }


def _save_publication(company, publication: dict) -> dict:
    update_ai_preferences(company, ai_updates={PUBLIC_VENDOR_AI_KEY: publication})
    db.session.flush()
    return publication


def publication_status(company) -> dict:
    publication = _publication_from_company(company)
    enabled = bool(get_ai_preferences(company).get("ai_agent", {}).get("public_webchat_enabled", False))
    publication["enabled"] = enabled
    publication["available"] = bool(publication["published"] and enabled)
    publication["url"] = (
        url_for("vendor_publication.public_vendor_page", slug=publication["slug"], _external=True)
        if publication["slug"]
        else None
    )
    return publication


def publish_vendor(company) -> dict:
    publication = _publication_from_company(company)
    if not publication["slug"]:
        publication["slug"] = _new_slug()
    publication["published"] = True
    publication["published_at"] = publication["published_at"] or _now_iso()
    _save_publication(company, publication)
    update_ai_preferences(company, ai_updates={"public_webchat_enabled": True})
    return publication_status(company)


def unpublish_vendor(company) -> dict:
    publication = _publication_from_company(company)
    publication["published"] = False
    _save_publication(company, publication)
    update_ai_preferences(company, ai_updates={"public_webchat_enabled": False})
    return publication_status(company)


def regenerate_vendor_link(company) -> dict:
    publication = _publication_from_company(company)
    was_published = bool(publication["published"])
    publication["slug"] = _new_slug()
    publication["rotated_at"] = _now_iso()
    publication["published"] = was_published
    if was_published:
        publication["published_at"] = _now_iso()
    _save_publication(company, publication)
    update_ai_preferences(company, ai_updates={"public_webchat_enabled": was_published})
    return publication_status(company)


def qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _resolve_company(slug: str):
    slug = str(slug or "").strip()
    if not slug:
        return None
    from app import Company

    marker = f'"slug":"{slug}"'
    candidates = Company.query.filter(Company.active.is_(True), Company.preferences_json.contains(marker)).all()
    for company in candidates:
        publication = _publication_from_company(company)
        if publication["slug"] == slug:
            return company
    return None


def _public_available_company(slug: str):
    company = _resolve_company(slug)
    if company is None:
        return None
    publication = _publication_from_company(company)
    enabled = bool(get_ai_preferences(company).get("ai_agent", {}).get("public_webchat_enabled", False))
    if not publication["published"] or not enabled:
        return None
    return company


def _visitor_id(company_id: int) -> str:
    from flask import session
    key = f"public_vendor_visitor_{int(company_id)}"
    import uuid
    value = str(session.get(key) or uuid.uuid4().hex)
    session[key] = value
    return value


def _rate_limit(company_id: int) -> bool:
    # Reuse the existing public Vendedor guard when available.
    try:
        from ai_agents import _public_vendor_rate_limit
        return _public_vendor_rate_limit(company_id)
    except Exception:
        return True


def public_vendor_page(slug: str):
    company = _public_available_company(slug)
    if company is None:
        abort(404)
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return render_template("ai_agents/public_vendor_chat.html", company=company, disabled_reason=access.reason, chat_url=None)
    _visitor_id(company.id)
    return render_template(
        "ai_agents/public_vendor_chat.html",
        company=company,
        disabled_reason=None,
        chat_url=url_for("vendor_publication.public_vendor_message", slug=slug, _external=True),
    )


def public_vendor_message(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    if not _rate_limit(company.id):
        return jsonify({"success": False, "error": "Hay muchas consultas en este momento. Esperá unos segundos e intentá nuevamente."}), 429, {"Retry-After": "60"}

    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Escribí una consulta."}), 400
    if len(message) > 500:
        return jsonify({"success": False, "error": "La consulta es demasiado larga."}), 400

    from flask import session
    visitor_id = _visitor_id(company.id)
    agents = ensure_default_agents(company.id)
    vendor_agent = agents[VENDOR_AGENT_NAME]
    conversation_id = payload.get("conversation_id")
    if conversation_id not in (None, ""):
        try:
            conversation = Conversation.query.filter_by(
                id=int(conversation_id),
                company_id=company.id,
                channel="webchat",
                external_conversation_id=visitor_id,
            ).first()
        except (TypeError, ValueError):
            conversation = None
        if conversation is None:
            return jsonify({"success": False, "error": "La conversación ya no es válida."}), 403
        if conversation.agent_id != vendor_agent.id:
            return jsonify({"success": False, "error": "La conversación no corresponde al vendedor."}), 409
    else:
        conversation = Conversation.query.filter_by(
            company_id=company.id,
            channel="webchat",
            external_conversation_id=visitor_id,
            agent_id=vendor_agent.id,
        ).filter(Conversation.status == "open").order_by(Conversation.id.desc()).first()
        if conversation is None:
            conversation = Conversation(
                company_id=company.id,
                agent_id=vendor_agent.id,
                channel="webchat",
                external_conversation_id=visitor_id,
                status="open",
                metadata_json={"source": "public_webchat_stable"},
            )
            db.session.add(conversation)
            db.session.flush()

    from services.ai_agent.orchestrator_v2 import AgentRuntime
    try:
        result = AgentRuntime.process(
            company_id=company.id,
            conversation_id=conversation.id,
            message=message,
            channel="webchat",
            sender_id=None,
            idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or secrets.token_urlsafe(18)),
            metadata={"from": visitor_id, "source": "public_webchat_stable", "public_vendor_slug": slug},
            include_system_prompt=True,
        )
        return jsonify({
            "success": True,
            "conversation_id": result.get("conversation_id"),
            "message_id": result.get("message_id"),
            "assistant_message_id": result.get("assistant_message_id"),
            "content": result.get("content"),
        })
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Stable public Vendor IA failed company_id=%s", company.id)
        return jsonify({"success": False, "error": "No se pudo procesar la consulta. Intentá nuevamente."}), 500


@company_admin_required
def publication_page():
    company = current_user.company
    status = publication_status(company)
    access = can_use_ai(company, "vendedor")
    preview_url = url_for("vendor_publication.preview_vendor", _external=True)
    return render_template(
        "ai_agents/vendor_publication.html",
        company=company,
        status=status,
        access=access,
        preview_url=preview_url,
        qr=qr_data_uri(status["url"]) if status["url"] else None,
    )


@company_admin_required
def publication_action(action: str):
    company = current_user.company
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    action = str(action or "").strip().lower()
    if action == "publish":
        status = publish_vendor(company)
        message = "Vendedor IA publicado."
    elif action == "unpublish":
        status = unpublish_vendor(company)
        message = "Vendedor IA despublicado."
    elif action == "regenerate":
        status = regenerate_vendor_link(company)
        message = "Enlace público regenerado."
    else:
        return jsonify({"success": False, "error": "Acción inválida."}), 400
    db.session.commit()
    status["message"] = message
    status["qr"] = qr_data_uri(status["url"]) if status["url"] else None
    return jsonify({"success": True, **status})


@company_admin_required
def preview_vendor():
    company = current_user.company
    return render_template(
        "ai_agents/public_vendor_chat.html",
        company=company,
        disabled_reason="Vista previa del Vendedor IA. Publicá el Vendedor para habilitar conversaciones públicas.",
        chat_url=None,
    )


def install_routes(app) -> None:
    if getattr(app, "_vendor_publication_routes_installed", False):
        return
    app.add_url_rule("/vendedor/<slug>", endpoint="vendor_publication.public_vendor_page", view_func=public_vendor_page, methods=["GET"])
    app.add_url_rule("/vendedor/<slug>/message", endpoint="vendor_publication.public_vendor_message", view_func=public_vendor_message, methods=["POST"])
    app.add_url_rule("/agentes-ia/vendedor/publicacion", endpoint="vendor_publication.publication_page", view_func=publication_page, methods=["GET"])
    app.add_url_rule("/agentes-ia/vendedor/publicacion/<action>", endpoint="vendor_publication.publication_action", view_func=publication_action, methods=["POST"])
    app.add_url_rule("/agentes-ia/vendedor/publicacion/preview", endpoint="vendor_publication.preview_vendor", view_func=preview_vendor, methods=["GET"])
    app._vendor_publication_routes_installed = True


@appcontext_pushed.connect
def _install_vendor_publication_routes(sender, **extra):
    try:
        install_routes(sender)
    except Exception:
        sender.logger.exception("No se pudieron registrar las rutas de publicación del Vendedor IA.")
