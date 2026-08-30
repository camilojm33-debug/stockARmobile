"""Commercial workflow used by the 24h vendor agent."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable
from urllib.parse import urlencode, urlsplit, urlunsplit

from flask import current_app
from sqlalchemy import or_

from stockarmobile.extensions import db
from services.mercadopago_service import MercadoPagoService
from services.mercadopago_oauth_service import MercadoPagoOAuthService
from services.sales.inventory_service import InventoryService
from services.sales.pricing_service import PricingService


FLOW_PREFIX = "flow:ai_order"
CART_KEY = "vendor_cart"
PENDING_QUOTE_KEY = "pending_quote_id"
PENDING_PAYMENT_KEY = "pending_payment_url"
MAX_CART_LINES = 30


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except Exception as exc:
        raise ValueError("Importe inválido.") from exc


def _normalize_phone(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _metadata(conversation) -> Dict[str, Any]:
    raw = conversation.metadata_json or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _set_metadata(conversation, data: Dict[str, Any]) -> None:
    conversation.metadata_json = data


def _public_quote_url(quote_id: int) -> str:
    from app import Quote
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer(current_app.config.get("SECRET_KEY", "stockarmobile-dev-secret"))
    token = serializer.dumps({"quote_id": int(quote_id)}, salt="quotes-public-share-v1")
    return f"{current_app.config.get('APP_URL', '').rstrip('/')}/presupuestos/publico/{token}"


def _with_payment_query(url: str, **params: str) -> str:
    parts = urlsplit(url)
    current = {}
    if parts.query:
        for pair in parts.query.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                current[key] = value
    current.update({k: str(v) for k, v in params.items()})
    query = urlencode(current)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _search_candidates(company_id: int, query: str):
    from app import Product

    normalized = _normalize_text(query)
    if not normalized:
        return []
    tokens = [token for token in re.findall(r"[a-z0-9áéíóúüñ]+", normalized) if len(token) >= 2]
    base = Product.query.filter(Product.company_id == company_id, Product.active.is_(True))
    if tokens:
        conditions = []
        for token in tokens[:5]:
            like = f"%{token}%"
            conditions.extend([
                Product.name.ilike(like),
                Product.barcode.ilike(like),
                Product.brand.ilike(like),
                Product.category.ilike(like),
            ])
        rows = base.filter(or_(*conditions)).order_by(Product.favorite.desc(), Product.name.asc()).limit(12).all()
    else:
        rows = base.order_by(Product.favorite.desc(), Product.name.asc()).limit(12).all()

    def score(product):
        haystack = _normalize_text(" ".join([
            str(product.name or ""),
            str(product.barcode or ""),
            str(product.brand or ""),
            str(product.category or ""),
        ]))
        score_value = 0
        if haystack == normalized:
            score_value += 1000
        if str(product.name or "").strip().lower() == normalized:
            score_value += 800
        for token in tokens:
            if token in _normalize_text(product.name):
                score_value += 100
            if token in haystack:
                score_value += 10
        return score_value

    return sorted(rows, key=lambda item: (-score(item), item.name.lower()))


class VendorOrderService:
    """Owns tenant-scoped cart, quote and payment transitions."""

    @staticmethod
    def get_cart(*, company_id: int, conversation_id: int) -> Dict[str, Any]:
        from stockarmobile.models.conversations import Conversation
        from app import Product

        conversation = db.session.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.company_id == company_id,
        ).first()
        if conversation is None:
            raise ValueError("Conversación no encontrada para esta empresa.")

        state = _metadata(conversation)
        raw_cart = state.get(CART_KEY) or {}
        if not isinstance(raw_cart, dict):
            raw_cart = {}

        ids = []
        for key in raw_cart:
            try:
                ids.append(int(key))
            except (TypeError, ValueError):
                continue
        products = {
            row.id: row
            for row in Product.query.filter(Product.company_id == company_id, Product.id.in_(sorted(set(ids)))).all()
        } if ids else {}

        items = []
        total = Decimal("0.00")
        for key, raw_qty in raw_cart.items():
            try:
                product_id = int(key)
                quantity = Decimal(str(raw_qty))
            except (TypeError, ValueError, ArithmeticError):
                continue
            product = products.get(product_id)
            if product is None or quantity <= 0:
                continue
            price = _money(product.price)
            subtotal = (price * quantity).quantize(Decimal("0.01"))
            total += subtotal
            items.append({
                "product_id": product.id,
                "name": product.name,
                "quantity": float(quantity),
                "unit_measure": product.unit_measure or "u",
                "unit_price": float(price),
                "stock": float(product.stock or 0),
                "subtotal": float(subtotal),
            })

        return {"items": items, "total": float(total), "currency": "ARS", "line_count": len(items)}

    @staticmethod
    def update_cart(
        *,
        company_id: int,
        conversation_id: int,
        items: Iterable[Dict[str, Any]] | None = None,
        clear: bool = False,
    ) -> Dict[str, Any]:
        from stockarmobile.models.conversations import Conversation
        from app import Product

        conversation = db.session.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.company_id == company_id,
        ).first()
        if conversation is None:
            raise ValueError("Conversación no encontrada para esta empresa.")

        state = _metadata(conversation)
        cart = {} if clear else dict(state.get(CART_KEY) or {})
        if not isinstance(items, list) and items is not None:
            raise ValueError("Los productos del carrito son inválidos.")
        if items is None:
            items = []

        for row in items:
            if not isinstance(row, dict):
                continue
            query = str(row.get("product_query") or row.get("query") or "").strip()
            quantity_raw = row.get("quantity", 1)
            try:
                quantity = Decimal(str(quantity_raw))
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise ValueError(f"Cantidad inválida para {query or 'producto'}.") from exc
            if quantity <= 0:
                raise ValueError("La cantidad debe ser mayor a cero.")

            product_id_raw = row.get("product_id")
            product = None
            if product_id_raw not in (None, ""):
                try:
                    product = Product.query.filter_by(id=int(product_id_raw), company_id=company_id, active=True).first()
                except (TypeError, ValueError):
                    product = None
            if product is None:
                candidates = _search_candidates(company_id, query)
                if not candidates:
                    raise ValueError(f"No encontré el producto '{query or 'solicitado'}'.")
                if len(candidates) > 1:
                    first, second = candidates[0], candidates[1]
                    first_name = _normalize_text(first.name)
                    second_name = _normalize_text(second.name)
                    if first_name != _normalize_text(query) and second_name != _normalize_text(query) and abs(_normalize_product_score(first, query) - _normalize_product_score(second, query)) < 20:
                        options = [
                            {"product_id": item.id, "name": item.name, "price": float(item.price or 0), "stock": float(item.stock or 0)}
                            for item in candidates[:5]
                        ]
                        return {"success": False, "error": "producto_ambiguo", "query": query, "candidates": options}
                product = candidates[0]

            available = Decimal(str(product.stock or 0))
            existing = Decimal(str(cart.get(str(product.id), 0) or 0))
            requested_total = existing + quantity
            if requested_total > available:
                raise ValueError(
                    f"Stock insuficiente para {product.name}. Disponible: {product.stock:g}; solicitado: {requested_total:g}."
                )
            if str(product.id) not in cart and len(cart) >= MAX_CART_LINES:
                raise ValueError(f"El carrito admite hasta {MAX_CART_LINES} productos diferentes.")
            cart[str(product.id)] = float(requested_total)

        # Remove zero/invalid lines and refresh state.
        cart = {key: qty for key, qty in cart.items() if float(qty or 0) > 0}
        state[CART_KEY] = cart
        state.pop(PENDING_QUOTE_KEY, None)
        state.pop(PENDING_PAYMENT_KEY, None)
        _set_metadata(conversation, state)
        db.session.flush()
        return VendorOrderService.get_cart(company_id=company_id, conversation_id=conversation_id)

    @staticmethod
    def remove_from_cart(*, company_id: int, conversation_id: int, product_query: str) -> Dict[str, Any]:
        from stockarmobile.models.conversations import Conversation
        from app import Product

        conversation = db.session.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.company_id == company_id,
        ).first()
        if conversation is None:
            raise ValueError("Conversación no encontrada para esta empresa.")
        state = _metadata(conversation)
        cart = dict(state.get(CART_KEY) or {})
        candidates = _search_candidates(company_id, product_query)
        selected = next((row for row in candidates if str(row.id) in cart), None)
        if selected is None:
            return VendorOrderService.get_cart(company_id=company_id, conversation_id=conversation_id)
        cart.pop(str(selected.id), None)
        state[CART_KEY] = cart
        _set_metadata(conversation, state)
        db.session.flush()
        return VendorOrderService.get_cart(company_id=company_id, conversation_id=conversation_id)

    @staticmethod
    def create_pending_order(
        *,
        company_id: int,
        conversation_id: int,
        customer_name: str = "",
        customer_phone: str = "",
        actor_user_id: int | None = None,
    ) -> Dict[str, Any]:
        from stockarmobile.models.conversations import Conversation
        from app import Client, Payment, Product, Quote, QuoteItem, User

        conversation = db.session.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.company_id == company_id,
        ).first()
        if conversation is None:
            raise ValueError("Conversación no encontrada para esta empresa.")

        cart = VendorOrderService.get_cart(company_id=company_id, conversation_id=conversation_id)
        if not cart["items"]:
            raise ValueError("El carrito está vacío.")

        state = _metadata(conversation)
        pending_quote_id = state.get(PENDING_QUOTE_KEY)
        if pending_quote_id:
            try:
                existing = Quote.query.filter_by(id=int(pending_quote_id), company_id=company_id).first()
            except (TypeError, ValueError):
                existing = None
            if existing is not None and existing.status not in {"ANULADO", "RECHAZADO", "VENCIDO", "CONVERTIDO"}:
                payment = Payment.query.filter_by(external_reference=f"{FLOW_PREFIX}|company_id:{company_id}|quote_id:{existing.id}").first()
                if payment is not None and payment.status in {"pending", "in_process"}:
                    return {
                        "success": True,
                        "existing": True,
                        "quote_id": existing.id,
                        "quote_number": existing.number or f"P-{existing.id:06d}",
                        "total": float(existing.total_amount or 0),
                        "payment_url": state.get(PENDING_PAYMENT_KEY) or "",
                        "quote_url": _public_quote_url(existing.id),
                    }

        actor = None
        if actor_user_id:
            actor = User.query.filter_by(id=int(actor_user_id), company_id=company_id, active=True).first()
        if actor is None:
            actor = User.query.filter(User.company_id == company_id, User.active.is_(True), User.role.in_(["admin", "user"])).order_by(User.id.asc()).first()
        if actor is None:
            raise ValueError("No hay un usuario activo para registrar el pedido.")

        phone = _normalize_phone(customer_phone)
        client = None
        if phone:
            client = Client.query.filter(
                Client.company_id == company_id,
                or_(Client.whatsapp == customer_phone, Client.phone == customer_phone, Client.whatsapp == phone, Client.phone == phone),
                Client.active.is_(True),
            ).order_by(Client.id.asc()).first()
        if client is None and customer_name.strip():
            clean_name = customer_name.strip()[:200]
            client = Client(
                name=clean_name,
                phone=phone[:20] or None,
                whatsapp=phone[:30] or None,
                company_id=company_id,
                active=True,
            )
            db.session.add(client)
            db.session.flush()
        elif client is not None and customer_name.strip() and client.name == "Consumidor final":
            client.name = customer_name.strip()[:200]

        product_ids = [int(item["product_id"]) for item in cart["items"]]
        products = {
            product.id: product
            for product in Product.query.filter(Product.company_id == company_id, Product.id.in_(product_ids), Product.active.is_(True)).all()
        }
        line_inputs = []
        for item in cart["items"]:
            product = products.get(int(item["product_id"]))
            if product is None:
                raise ValueError("Uno de los productos del carrito ya no está disponible.")
            quantity = Decimal(str(item["quantity"]))
            if quantity <= 0 or Decimal(str(product.stock or 0)) < quantity:
                raise ValueError(f"Stock insuficiente para {product.name}.")
            line_inputs.append({
                "price": _money(product.price),
                "quantity": quantity,
                "line_discount": _money(getattr(product, "discount", 0)),
            })

        totals = PricingService.calculate(lines=line_inputs, data={})
        quote = Quote(
            date=__import__("datetime").datetime.utcnow(),
            expires_at=__import__("datetime").datetime.utcnow() + timedelta(hours=24),
            subtotal=totals["subtotal"],
            discount=totals["line_discount_total"] + totals["general_discount"],
            surcharge=totals["surcharge"],
            tax=totals["tax"],
            total_amount=totals["total"],
            discount_type=totals["discount_adjustment"]["type"],
            discount_value=totals["discount_adjustment"]["value"],
            discount_reason=totals["discount_adjustment"]["reason"],
            surcharge_type=totals["surcharge_adjustment"]["type"],
            surcharge_value=totals["surcharge_adjustment"]["value"],
            surcharge_reason=totals["surcharge_adjustment"]["reason"],
            observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
            commercial_conditions="Pago mediante Mercado Pago. El stock se descuenta al confirmarse el pago.",
            status="ENVIADO",
            currency="ARS",
            client_id=client.id if client else None,
            consumer_name=None if client else (customer_name.strip()[:160] or "Consumidor final"),
            created_by_user_id=actor.id,
            seller_id=actor.id,
            company_id=company_id,
        )
        db.session.add(quote)
        db.session.flush()
        quote.number = f"P-{quote.id:06d}"

        for index, item in enumerate(cart["items"]):
            product = products[int(item["product_id"])]
            quantity = Decimal(str(item["quantity"]))
            unit_price = _money(product.price)
            discount = (_money(product.discount) * quantity).quantize(Decimal("0.01"))
            subtotal = max((unit_price * quantity) - discount, Decimal("0.00"))
            db.session.add(
                QuoteItem(
                    quote_id=quote.id,
                    product_id=product.id,
                    description=product.name,
                    quantity=float(quantity),
                    unit_price=unit_price,
                    discount=discount,
                    subtotal=subtotal,
                    sort_order=index,
                )
            )
        db.session.flush()

        external_reference = "|".join([
            FLOW_PREFIX,
            f"company_id:{company_id}",
            f"quote_id:{quote.id}",
            f"conversation_id:{conversation_id}",
            f"user_id:{actor.id}",
        ])
        oauth = MercadoPagoOAuthService()
        access_token = oauth.ensure_access_token(company_id=company_id)
        mp = MercadoPagoService()
        quote_url = _public_quote_url(quote.id)
        result = mp.create_ai_order_checkout_preference(
            title=f"Pedido {quote.number} - StockARmobile",
            items=[
                {
                    "id": str(product.id),
                    "title": product.name,
                    "description": product.name,
                    "quantity": int(item["quantity"]) if float(item["quantity"]).is_integer() else float(item["quantity"]),
                    "currency_id": quote.currency or "ARS",
                    "unit_price": float(_money(product.price)),
                }
                for item in cart["items"]
                for product in [products[int(item["product_id"])] ]
            ],
            amount=float(quote.total_amount or 0),
            currency=quote.currency or "ARS",
            external_reference=external_reference,
            company_id=company_id,
            user_id=actor.id,
            quote_id=quote.id,
            conversation_id=conversation_id,
            return_url=_with_payment_query(quote_url, payment="completed"),
            access_token=access_token,
        )
        payment_url = str(result.get("init_point") or result.get("sandbox_init_point") or "").strip()
        if not payment_url:
            raise RuntimeError("Mercado Pago no devolvió un link de pago.")

        payment = Payment(
            payment_id=None,
            preference_id=str(result.get("id") or "").strip() or None,
            external_reference=external_reference,
            company_id=company_id,
            subscription_id=None,
            user_id=actor.id,
            amount=quote.total_amount,
            currency=quote.currency or "ARS",
            status="pending",
            payment_method="mercadopago_ai_order",
            provider="mercadopago_ai_order",
            reference=external_reference,
            payload_json=json.dumps(result, ensure_ascii=False),
        )
        db.session.add(payment)
        state[PENDING_QUOTE_KEY] = quote.id
        state[PENDING_PAYMENT_KEY] = payment_url
        state["customer_phone"] = phone or customer_phone
        if customer_name.strip():
            state["customer_name"] = customer_name.strip()[:160]
        _set_metadata(conversation, state)
        db.session.commit()

        return {
            "success": True,
            "quote_id": quote.id,
            "quote_number": quote.number,
            "total": float(quote.total_amount or 0),
            "currency": quote.currency or "ARS",
            "payment_url": payment_url,
            "quote_url": quote_url,
            "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
        }

    @staticmethod
    def finalize_paid_order(*, company_id: int, quote_id: int, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        from app import Payment, Product, Quote, Sale, SaleItem, db as app_db

        quote = Quote.query.filter_by(id=int(quote_id), company_id=int(company_id)).first()
        if quote is None:
            raise ValueError("Presupuesto del pedido no encontrado para esta empresa.")

        payment_id = str(payment_data.get("id") or "").strip()
        amount = _money(payment_data.get("transaction_amount"))
        currency = str(payment_data.get("currency_id") or quote.currency or "ARS").upper()
        if currency != str(quote.currency or "ARS").upper():
            raise ValueError("La moneda del pago no coincide con el pedido.")
        expected = _money(quote.total_amount)
        if amount != expected:
            raise ValueError(f"El importe aprobado ({amount}) no coincide con el pedido ({expected}).")

        payment = Payment.query.filter_by(payment_id=payment_id).first() if payment_id else None
        if payment is None:
            external_reference = str(payment_data.get("external_reference") or "")
            payment = Payment(
                payment_id=payment_id or None,
                preference_id=str(payment_data.get("order", {}).get("id") or "") or None,
                external_reference=external_reference,
                company_id=company_id,
                amount=amount,
                currency=currency,
                status="approved",
                payment_method=str(payment_data.get("payment_method_id") or "mercadopago"),
                provider="mercadopago_ai_order",
                reference=external_reference or payment_id,
                payload_json=json.dumps(payment_data, ensure_ascii=False),
                paid_at=__import__("datetime").datetime.utcnow(),
            )
            app_db.session.add(payment)
        else:
            if int(payment.company_id or 0) != int(company_id):
                raise ValueError("El pago no pertenece a la empresa del pedido.")
            payment.status = "approved"
            payment.amount = amount
            payment.currency = currency
            payment.payment_method = str(payment_data.get("payment_method_id") or payment.payment_method or "mercadopago")
            payment.payload_json = json.dumps(payment_data, ensure_ascii=False)
            payment.paid_at = __import__("datetime").datetime.utcnow()

        if quote.converted_sale_id:
            sale = Sale.query.filter_by(id=quote.converted_sale_id, company_id=company_id).first()
            return {
                "status": "already_converted",
                "sale_id": sale.id if sale else quote.converted_sale_id,
                "quote_id": quote.id,
                "quote_number": quote.number,
                "total": float(quote.total_amount or 0),
            }

        item_rows = list(quote.items)
        if not item_rows:
            raise ValueError("El pedido no tiene líneas.")
        product_ids = sorted({int(item.product_id) for item in item_rows if item.product_id})
        products = {
            product.id: product
            for product in Product.query.filter(Product.company_id == company_id, Product.id.in_(product_ids), Product.active.is_(True)).with_for_update().all()
        }
        lines = []
        for item in item_rows:
            product = products.get(int(item.product_id or 0))
            if product is None:
                raise ValueError("Uno de los productos del pedido ya no existe.")
            quantity = Decimal(str(item.quantity or 0))
            if quantity <= 0:
                raise ValueError("El pedido contiene una cantidad inválida.")
            if Decimal(str(product.stock or 0)) < quantity:
                raise ValueError(f"Stock insuficiente para completar {product.name} después del pago.")
            lines.append({"product": product, "quantity": float(quantity), "price": _money(item.unit_price), "discount": _money(item.discount)})

        sale = Sale(
            date=__import__("datetime").datetime.utcnow(),
            customer=quote.client.name if quote.client is not None else (quote.consumer_name or "Consumidor final"),
            subtotal=_money(quote.subtotal),
            discount=_money(quote.discount),
            tax=_money(quote.tax),
            total_amount=expected,
            payment_method="MERCADO_PAGO",
            secondary_payment_method=None,
            paid_amount=expected,
            secondary_paid_amount=Decimal("0.00"),
            surcharge=_money(quote.surcharge),
            discount_type=quote.discount_type,
            discount_value=quote.discount_value,
            discount_reason=quote.discount_reason,
            surcharge_type=quote.surcharge_type,
            surcharge_value=quote.surcharge_value,
            surcharge_reason=quote.surcharge_reason,
            client_txn_id=f"ai-order-quote:{quote.id}",
            document_type="venta",
            requiere_comprobante=False,
            comprobante_emitido=False,
            status="confirmada",
            note=f"Venta generada automáticamente por Vendedor 24 hs. Presupuesto {quote.number or quote.id}.",
            client_id=quote.client_id,
            seller_id=quote.seller_id or quote.created_by_user_id,
            company_id=company_id,
            cash_session_id=None,
        )
        app_db.session.add(sale)
        app_db.session.flush()
        InventoryService.apply_stock(lines=lines)

        for item, line in zip(item_rows, lines):
            app_db.session.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=line["product"].id,
                    quantity=line["quantity"],
                    price=line["price"],
                    cost_price=_money(line["product"].cost_price),
                    discount=_money(item.discount),
                )
            )

        quote.status = "CONVERTIDO"
        quote.converted_sale_id = sale.id
        app_db.session.commit()
        return {
            "status": "converted",
            "sale_id": sale.id,
            "quote_id": quote.id,
            "quote_number": quote.number or f"P-{quote.id:06d}",
            "total": float(expected),
        }


def _normalize_product_score(product, query: str) -> int:
    normalized = _normalize_text(query)
    name = _normalize_text(product.name)
    score = 0
    if name == normalized:
        score += 1000
    for token in re.findall(r"[a-z0-9áéíóúüñ]+", normalized):
        if token in name:
            score += 100
        elif token in _normalize_text(product.brand):
            score += 30
    return score
