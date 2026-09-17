"""Public checkout/order-state hardening for the Vendor IA mini store."""
from __future__ import annotations

from flask import jsonify, request, current_app
from flask.signals import appcontext_pushed

from services.ai_agent.usage_service import can_use_ai
from services.ai_agent.vendor_order_service import VendorOrderService
from services.ai_agent.vendor_publication import (
    _cart_public_state,
    _public_available_company,
    _public_conversation,
    _rate_limit,
)
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation


PUBLIC_POST_MAX_BYTES = 16 * 1024
_PUBLIC_MUTATION_ENDPOINTS = {"cart", "checkout", "order/retry", "order/cancel"}


def _payload() -> dict:
    return request.get_json(silent=True) or {}


def _order_context(company, payload: dict):
    conversation = _public_conversation(
        company,
        payload.get("conversation_id"),
        create=False,
    )
    if conversation is None:
        return None, (jsonify({"success": False, "error": "La conversación no es válida."}), 403)
    return conversation, None


def _lock_public_conversation(company, conversation_id):
    """Serialize public mutations for the exact tenant-scoped conversation."""
    try:
        return (
            db.session.query(Conversation)
            .filter(
                Conversation.id == int(conversation_id),
                Conversation.company_id == int(company.id),
                Conversation.channel == "webchat",
            )
            .with_for_update()
            .first()
        )
    except (TypeError, ValueError):
        return None


def _public_order_error(exc: Exception, fallback: str):
    if isinstance(exc, ValueError):
        return jsonify({"success": False, "error": str(exc)}), 400
    current_app.logger.exception("Public Vendor checkout action failed")
    return jsonify({"success": False, "error": fallback}), 500


def public_vendor_order_status(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403

    conversation = _public_conversation(
        company,
        request.args.get("conversation_id"),
        create=False,
    )
    if conversation is None:
        return jsonify({"success": True, "found": False, "conversation_id": None}), 200

    try:
        result = VendorOrderService.get_customer_order_status(
            company_id=company.id,
            conversation_id=conversation.id,
            order_number=str(request.args.get("order_number") or "").strip(),
        )
        return jsonify({"success": True, "conversation_id": conversation.id, **result})
    except Exception as exc:
        return _public_order_error(exc, "No se pudo consultar el estado del pedido.")


def public_vendor_retry_payment(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403

    payload = _payload()
    conversation, error = _order_context(company, payload)
    if error:
        return error
    try:
        result = VendorOrderService.retry_payment(
            company_id=company.id,
            conversation_id=conversation.id,
            order_number=str(payload.get("order_number") or "").strip(),
        )
        db_state = _cart_public_state(conversation)
        return jsonify({"success": bool(result.get("success", True)), "conversation_id": conversation.id, "cart": db_state["cart"], **result})
    except Exception as exc:
        return _public_order_error(exc, "No se pudo generar un nuevo link de pago.")


def public_vendor_cancel_order(slug: str):
    company = _public_available_company(slug)
    if company is None:
        return jsonify({"success": False, "error": "El vendedor no está publicado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403

    payload = _payload()
    conversation, error = _order_context(company, payload)
    if error:
        return error
    try:
        confirm = bool(
            payload.get("confirm") is True
            or str(payload.get("confirm") or "").strip().lower() in {"1", "true", "si", "sí", "yes"}
        )
        result = VendorOrderService.cancel_order(
            company_id=company.id,
            conversation_id=conversation.id,
            order_number=str(payload.get("order_number") or "").strip(),
            confirm=confirm,
        )
        return jsonify({"success": bool(result.get("success", False)), "conversation_id": conversation.id, **result})
    except Exception as exc:
        return _public_order_error(exc, "No se pudo cancelar el pedido.")


def _guard_public_mutations() -> object | None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    if request.content_length is not None and request.content_length > PUBLIC_POST_MAX_BYTES:
        return jsonify({"success": False, "error": "La solicitud es demasiado grande."}), 413

    # Also enforce the limit for chunked requests without Content-Length.
    raw_body = request.get_data(cache=True, as_text=False)
    if len(raw_body) > PUBLIC_POST_MAX_BYTES:
        return jsonify({"success": False, "error": "La solicitud es demasiado grande."}), 413

    parts = [part for part in request.path.strip("/").split("/") if part]
    if len(parts) < 3 or parts[0] != "vendedor":
        return None

    endpoint_key = "/".join(parts[2:])
    if endpoint_key not in _PUBLIC_MUTATION_ENDPOINTS:
        return None

    try:
        company = _public_available_company(parts[1])
        if company is None:
            return None
        if not _rate_limit(company.id):
            return jsonify({"success": False, "error": "Hay muchas consultas en este momento. Esperá unos segundos e intentá nuevamente."}), 429, {"Retry-After": "60"}

        payload = _payload()
        conversation = _public_conversation(company, payload.get("conversation_id"), create=False)
        if conversation is not None:
            # Keep the row lock in the current SQLAlchemy transaction so concurrent
            # public cart/checkout/retry/cancel requests for this conversation serialize.
            locked = _lock_public_conversation(company, conversation.id)
            if locked is None:
                return jsonify({"success": False, "error": "La conversación ya no es válida."}), 403
    except Exception:
        current_app.logger.exception("No se pudo aplicar protección de concurrencia al Vendor IA público")
        return jsonify({"success": False, "error": "El canal público no está disponible temporalmente."}), 503
    return None


def install_routes(app) -> None:
    if getattr(app, "_vendor_checkout_public_routes_installed", False):
        return
    app.add_url_rule(
        "/vendedor/<slug>/order/status",
        endpoint="vendor_checkout_public.public_vendor_order_status",
        view_func=public_vendor_order_status,
        methods=["GET"],
    )
    app.add_url_rule(
        "/vendedor/<slug>/order/retry",
        endpoint="vendor_checkout_public.public_vendor_retry_payment",
        view_func=public_vendor_retry_payment,
        methods=["POST"],
    )
    app.add_url_rule(
        "/vendedor/<slug>/order/cancel",
        endpoint="vendor_checkout_public.public_vendor_cancel_order",
        view_func=public_vendor_cancel_order,
        methods=["POST"],
    )
    app.before_request(_guard_public_mutations)
    app._vendor_checkout_public_routes_installed = True


@appcontext_pushed.connect
def _install_vendor_checkout_routes(sender, **extra):
    try:
        install_routes(sender)
    except Exception:
        sender.logger.exception("No se pudieron registrar las rutas públicas de checkout del Vendedor IA.")
