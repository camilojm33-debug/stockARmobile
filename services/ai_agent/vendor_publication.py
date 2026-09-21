"""Stable public publication layer for each merchant's Vendor IA."""
from __future__ import annotations

import base64
import io
import json
import secrets
from datetime import datetime, timezone

import qrcode
from flask import jsonify, render_template, request, url_for, abort, current_app, session
from flask.signals import appcontext_pushed
from flask_login import current_user
from sqlalchemy import or_

from stockarmobile.decorators import company_admin_required
from stockarmobile.tenant import get_current_company_id
from stockarmobile.extensions import csrf, db
from stockarmobile.models.conversations import Conversation
from services.ai_agent.config_service import (
    get_ai_preferences,
    update_ai_preferences,
    ensure_default_agents,
    VENDOR_AGENT_NAME,
    get_vendor_options,
)
from services.ai_agent.usage_service import can_use_ai, can_use_ai_feature
from services.ai_agent.vendor_order_service import (
    CART_KEY,
    PENDING_PAYMENT_KEY,
    PENDING_QUOTE_KEY,
    VendorOrderService,
)


PUBLIC_VENDOR_AI_KEY = "public_vendor"
PUBLIC_VENDOR_SLUG_SIZE = 12
PUBLIC_VENDOR_CATALOG_LIMIT = 24


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


def _authenticated_company():
    """Resolve the authenticated tenant without relying on a nonexistent User.company relationship."""
    company_id = get_current_company_id(current_user)
    if company_id is None:
        abort(403)
    from app import Company

    company = Company.query.filter_by(id=int(company_id), active=True).first()
    if company is None:
        abort(403)
    return company


def _visitor_id(company_id: int) -> str:
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
        current_app.logger.exception("Public Vendor rate limiter unavailable; failing closed.")
        return False


def _agent_for_company(company_id: int):
    agents = ensure_default_agents(company_id)
    return agents[VENDOR_AGENT_NAME]


def _conversation_metadata(conversation) -> dict:
    raw = conversation.metadata_json or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _public_conversation(company, conversation_id=None, *, create=False):
    visitor_id = _visitor_id(company.id)
    vendor_agent = _agent_for_company(company.id)
    conversation = None

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
        if conversation is None or conversation.agent_id != vendor_agent.id:
            return None
    else:
        conversation = (
            Conversation.query.filter_by(
                company_id=company.id,
                channel="webchat",
                external_conversation_id=visitor_id,
                agent_id=vendor_agent.id,
            )
            .filter(Conversation.status == "open")
            .order_by(Conversation.id.desc())
            .first()
        )

    if conversation is None and create:
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
    return conversation


def _cart_public_state(conversation) -> dict:
    cart = VendorOrderService.get_cart(company_id=conversation.company_id, conversation_id=conversation.id)
    state = _conversation_metadata(conversation)
    payment_url = str(state.get(PENDING_PAYMENT_KEY) or "").strip()
    return {
        "conversation_id": conversation.id,
        "cart": cart,
        "payment_url": payment_url or None,
        "pending_quote_id": state.get(PENDING_QUOTE_KEY),
        "delivery": state.get("delivery") or None,
    }


def _product_payload(product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "description": str(product.description or "").strip(),
        "category": str(product.category or "").strip(),
        "brand": str(product.brand or "").strip(),
        "barcode": str(product.barcode or "").strip(),
        "photo": str(product.photo or "").strip(),
        "unit_measure": str(product.unit_measure or "u").strip(),
        "price": float(product.price or 0),
        "stock": float(product.stock or 0),
        "discount": float(product.discount or 0),
        "favorite": bool(product.favorite),
        "available": float(product.stock or 0) > 0,
    }


def _catalog_for_company(company, query: str = "", limit: int = PUBLIC_VENDOR_CATALOG_LIMIT) -> list[dict]:
    from app import Product

    query = " ".join(str(query or "").strip().split())[:80]
    limit = max(1, min(int(limit or PUBLIC_VENDOR_CATALOG_LIMIT), PUBLIC_VENDOR_CATALOG_LIMIT))
    base = Product.query.filter(Product.company_id == company.id, Product.active.is_(True))
    if query:
        like = f"%{query}%"
        base = base.filter(
            or_(
                Product.name.ilike(like),
                Product.barcode.ilike(like),
                Product.brand.ilike(like),
                Product.category.ilike(like),
            )
        )
    rows = (
        base.order_by(Product.favorite.desc(), Product.stock.desc(), Product.name.asc())
        .limit(limit)
        .all()
    )
    return [_product_payload(row) for row in rows]


def _initial_page_state(company):
    conversation = _public_conversation(company, create=False)
    if conversation is None:
        return {"conversation_id": None, "cart": {"items": [], "total": 0, "currency": "ARS", "line_count": 0}, "payment_url": None}
    try:
        return _cart_public_state(conversation)
    except Exception:
        current_app.logger.exception("No se pudo cargar el carrito público company_id=%s", company.id)
        return {"conversation_id": conversation.id, "cart": {"items": [], "total": 0, "currency": "ARS", "line_count": 0}, "payment_url": None}


def public_vendor_page(slug: str):
    company = _public_available_company(slug)
    if company is None:
        abort(404)
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return render_template(
            "ai_agents/public_vendor_chat.html",
            company=company,
            disabled_reason=access.reason,
            chat_url=None,
            catalog=[],
            initial_state={"conversation_id": None, "cart": {"items": [], "total": 0, "currency": "ARS", "line_count": 0}, "payment_url": None},
            greeting="",
        )
    _visitor_id(company.id)
    options = get_vendor_options(company)
    shipping_config = {
        "mode": options.get("shipping_mode", "legacy_percent"),
        "standard_cost": float(options.get("standard_shipping_cost") or 0),
    }
    return render_template(
        "ai_agents/public_vendor_chat.html",
        company=company,
        disabled_reason=None,
        chat_url=url_for("vendor_publication.public_vendor_message", slug=slug, _external=True),
        shipping_config=shipping_config,
        catalog=_catalog_for_company(company),
        initial_state=_initial_page_state(company),
        greeting=str(options.get("greeting") or "Hola 👋 ¿Qué producto estás buscando?").strip(),
    )


def public_vendor_catalog(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    query = request.args.get("q", "")
    return jsonify({"success": True, "products": _catalog_for_company(company, query=query)})


def public_vendor_state(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    conversation = _public_conversation(company, request.args.get("conversation_id"), create=False)
    if conversation is None:
        return jsonify({
            "success": True,
            "conversation_id": None,
            "cart": {"items": [], "total": 0, "currency": "ARS", "line_count": 0},
            "payment_url": None,
        })
    return jsonify({"success": True, **_cart_public_state(conversation)})


def public_vendor_cart(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403

    payload = request.get_json(silent=True) or {}
    conversation = _public_conversation(company, payload.get("conversation_id"), create=True)
    if conversation is None:
        return jsonify({"success": False, "error": "La conversación no es válida."}), 403

    action = str(payload.get("action") or "add").strip().lower()
    try:
        product_id = int(payload.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Producto inválido."}), 400

    try:
        if action == "add":
            try:
                quantity = float(payload.get("quantity", 1))
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0 or quantity > 1000:
                return jsonify({"success": False, "error": "Cantidad inválida."}), 400
            VendorOrderService.update_cart(
                company_id=company.id,
                conversation_id=conversation.id,
                items=[{"product_id": product_id, "quantity": quantity}],
            )
        elif action == "remove":
            state = _conversation_metadata(conversation)
            cart = dict(state.get(CART_KEY) or {})
            cart.pop(str(product_id), None)
            state[CART_KEY] = cart
            state.pop(PENDING_QUOTE_KEY, None)
            state.pop(PENDING_PAYMENT_KEY, None)
            conversation.metadata_json = state
            db.session.flush()
        else:
            return jsonify({"success": False, "error": "Acción inválida."}), 400
        db.session.commit()
        return jsonify({"success": True, **_cart_public_state(conversation)})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Public Vendor cart failed company_id=%s", company.id)
        return jsonify({"success": False, "error": "No se pudo actualizar el carrito."}), 500


def public_vendor_checkout(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403

    payload = request.get_json(silent=True) or {}
    conversation = _public_conversation(company, payload.get("conversation_id"), create=True)
    if conversation is None:
        return jsonify({"success": False, "error": "La conversación no es válida."}), 403
    customer_name = str(payload.get("customer_name") or "").strip()[:160]
    customer_phone = str(payload.get("customer_phone") or "").strip()[:40]
    if not customer_name or not customer_phone:
        return jsonify({"success": False, "error": "Ingresá nombre y teléfono para registrar tu pedido como cliente."}), 400
    delivery_method = str(payload.get("delivery_method") or "retiro").strip()[:20]
    delivery_address = str(payload.get("delivery_address") or "").strip()[:500]
    delivery_city = str(payload.get("delivery_city") or "").strip()[:100]
    delivery_province = str(payload.get("delivery_province") or "").strip()[:120]
    delivery_postal_code = str(payload.get("delivery_postal_code") or "").strip()[:20]
    delivery_reference = str(payload.get("delivery_reference") or "").strip()[:255]
    delivery_notes = str(payload.get("delivery_notes") or "").strip()[:2000]
    try:
        result = VendorOrderService.create_pending_order(
            company_id=company.id,
            conversation_id=conversation.id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            delivery_method=delivery_method,
            delivery_address=delivery_address,
            delivery_city=delivery_city,
            delivery_province=delivery_province,
            delivery_postal_code=delivery_postal_code,
            delivery_reference=delivery_reference,
            delivery_notes=delivery_notes,
            actor_user_id=None,
        )
        db.session.commit()
        return jsonify({"success": True, "conversation_id": conversation.id, "cart": _cart_public_state(conversation)["cart"], **result})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Public Vendor checkout failed company_id=%s", company.id)
        return jsonify({"success": False, "error": "No se pudo preparar el pedido para el pago."}), 500


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

    conversation = _public_conversation(company, payload.get("conversation_id"), create=True)
    if conversation is None:
        return jsonify({"success": False, "error": "La conversación ya no es válida."}), 403
    visitor_id = _visitor_id(company.id)
    try:
        from services.ai_agent.orchestrator_v2 import AgentRuntime

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
        state = _cart_public_state(conversation)
        return jsonify({
            "success": True,
            "conversation_id": result.get("conversation_id"),
            "message_id": result.get("message_id"),
            "assistant_message_id": result.get("assistant_message_id"),
            "content": result.get("content"),
            "cart": state["cart"],
            "payment_url": state["payment_url"],
        })
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Stable public Vendor IA failed company_id=%s", company.id)
        return jsonify({"success": False, "error": "No se pudo procesar la consulta. Intentá nuevamente."}), 500


# Public Vendedor endpoints are intentionally accessible without authenticated CSRF sessions.
# The signed slug, tenant scoping and rate limit remain the primary protections.
csrf.exempt(public_vendor_message)
csrf.exempt(public_vendor_catalog)
csrf.exempt(public_vendor_state)
csrf.exempt(public_vendor_cart)
csrf.exempt(public_vendor_checkout)

@company_admin_required
def publication_page():
    company = _authenticated_company()
    status = publication_status(company)
    access = can_use_ai_feature(company, "vendedor")
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
    company = _authenticated_company()
    access = can_use_ai_feature(company, "vendedor")
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
    company = _authenticated_company()
    return render_template(
        "ai_agents/public_vendor_chat.html",
        company=company,
        disabled_reason="Vista previa del Vendedor IA. Publicá el Vendedor para habilitar conversaciones públicas.",
        chat_url=None,
        shipping_config={"mode": "manual", "standard_cost": 0},
        catalog=_catalog_for_company(company),
        initial_state={"conversation_id": None, "cart": {"items": [], "total": 0, "currency": "ARS", "line_count": 0}, "payment_url": None},
        greeting="Hola 👋 ¿Qué producto estás buscando?",
    )


def install_routes(app) -> None:
    if getattr(app, "_vendor_publication_routes_installed", False):
        return
    app.add_url_rule("/vendedor/<slug>", endpoint="vendor_publication.public_vendor_page", view_func=public_vendor_page, methods=["GET"])
    app.add_url_rule("/vendedor/<slug>/catalog", endpoint="vendor_publication.public_vendor_catalog", view_func=public_vendor_catalog, methods=["GET"])
    app.add_url_rule("/vendedor/<slug>/state", endpoint="vendor_publication.public_vendor_state", view_func=public_vendor_state, methods=["GET"])
    app.add_url_rule("/vendedor/<slug>/cart", endpoint="vendor_publication.public_vendor_cart", view_func=public_vendor_cart, methods=["POST"])
    app.add_url_rule("/vendedor/<slug>/checkout", endpoint="vendor_publication.public_vendor_checkout", view_func=public_vendor_checkout, methods=["POST"])
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
