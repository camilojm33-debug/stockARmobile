import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest

import app as stock_app
from app import Client, Company, Payment, Product, Quote, QuoteDelivery, User, db
from quotes import _build_public_quote_accept_url, _build_public_quote_pdf_url, _build_public_quote_url, _build_quote_whatsapp_message, _quote_charge_display_rows
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.followup_service import AIFollowupService
from services.ai_agent.vendor_order_service import (
    CART_KEY,
    LAST_ORDER_KEY,
    PENDING_PAYMENT_KEY,
    PENDING_QUOTE_KEY,
    VendorOrderService,
)
from services.mercadopago_service import MercadoPagoService
from stockarmobile.models.conversations import Conversation


@pytest.fixture
def vendor_database():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    stock_app.app.config["APP_URL"] = "http://test.local"

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()

        company_a = Company(name="Empresa A", active=True)
        company_b = Company(name="Empresa B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()

        user_a = User(
            username="vendedor_a",
            email="vendedor.a@test.local",
            password_hash="test-password-hash",
            role="admin",
            active=True,
            company_id=company_a.id,
        )
        db.session.add(user_a)
        db.session.flush()

        product_a = Product(
            barcode="A-001",
            name="Cafe clasico",
            price=100,
            cost_price=50,
            stock=10,
            min_stock=1,
            active=True,
            company_id=company_a.id,
        )
        product_b = Product(
            barcode="B-001",
            name="Producto exclusivo B",
            price=200,
            cost_price=100,
            stock=20,
            min_stock=1,
            active=True,
            company_id=company_b.id,
        )
        client_a = Client(
            name="Cliente A",
            phone="5491112345678",
            whatsapp="5491112345678",
            active=True,
            company_id=company_a.id,
        )
        db.session.add_all([product_a, product_b, client_a])
        db.session.commit()

        yield {
            "company_a": company_a,
            "company_b": company_b,
            "user_a": user_a,
            "product_a": product_a,
            "product_b": product_b,
            "client_a": client_a,
        }

        db.session.remove()
        db.drop_all()


def _conversation(company_id):
    conversation = Conversation(company_id=company_id, channel="whatsapp", status="open")
    db.session.add(conversation)
    db.session.commit()
    return conversation


def _mock_checkout(monkeypatch, calls, result=None):
    def ensure_access_token(self, *, company_id):
        calls.append(("oauth", company_id))
        return "test-access-token"

    def create_preference(self, **kwargs):
        calls.append(("checkout", kwargs))
        return result if result is not None else {
            "id": "pref-test-001",
            "init_point": "https://payments.test/checkout/001",
        }

    monkeypatch.setattr(
        "services.ai_agent.vendor_order_service.MercadoPagoOAuthService.ensure_access_token",
        ensure_access_token,
    )
    monkeypatch.setattr(
        "services.ai_agent.vendor_order_service.MercadoPagoService.create_ai_order_checkout_preference",
        create_preference,
    )


def test_cart_is_company_scoped_and_starts_empty(vendor_database):
    data = vendor_database
    conversation_a = _conversation(data["company_a"].id)
    conversation_b = _conversation(data["company_b"].id)

    cart = VendorOrderService.get_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation_a.id,
    )

    assert cart == {"items": [], "total": 0.0, "currency": "ARS", "line_count": 0}
    with pytest.raises(ValueError, match="Conversaci.n no encontrada"):
        VendorOrderService.get_cart(
            company_id=data["company_a"].id,
            conversation_id=conversation_b.id,
        )


def test_update_cart_searches_product_and_reads_persisted_state(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)

    updated = VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )
    reloaded = VendorOrderService.get_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
    )

    assert updated == reloaded
    assert reloaded["line_count"] == 1
    assert reloaded["items"][0]["product_id"] == data["product_a"].id
    assert reloaded["items"][0]["quantity"] == 2.0
    assert reloaded["total"] == 200.0
    assert conversation.metadata_json[CART_KEY] == {str(data["product_a"].id): 2.0}


def test_update_cart_rejects_insufficient_stock(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)

    with pytest.raises(ValueError, match="Stock insuficiente para Cafe clasico"):
        VendorOrderService.update_cart(
            company_id=data["company_a"].id,
            conversation_id=conversation.id,
            items=[{"product_query": "Cafe clasico", "quantity": 11}],
        )


def test_update_cart_rejects_other_company_product_id(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)

    with pytest.raises(ValueError, match="No encontr. el producto"):
        VendorOrderService.update_cart(
            company_id=data["company_a"].id,
            conversation_id=conversation.id,
            items=[{"product_id": data["product_b"].id, "quantity": 1}],
        )


def test_remove_from_cart_deletes_matching_product(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )

    cart = VendorOrderService.remove_from_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        product_query="Cafe clasico",
    )

    assert cart["items"] == []
    assert cart["total"] == 0.0
    assert conversation.metadata_json[CART_KEY] == {}


def test_update_cart_reports_ambiguous_products(vendor_database):
    data = vendor_database
    db.session.add_all(
        [
            Product(
                barcode="A-002",
                name="Te verde",
                price=50,
                cost_price=20,
                stock=5,
                active=True,
                company_id=data["company_a"].id,
            ),
            Product(
                barcode="A-003",
                name="Te negro",
                price=60,
                cost_price=25,
                stock=5,
                active=True,
                company_id=data["company_a"].id,
            ),
        ]
    )
    db.session.commit()
    conversation = _conversation(data["company_a"].id)

    result = VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Te", "quantity": 1}],
    )

    assert result["success"] is False
    assert result["error"] == "producto_ambiguo"
    assert {candidate["name"] for candidate in result["candidates"]} >= {"Te verde", "Te negro"}
    assert CART_KEY not in (conversation.metadata_json or {})


def test_create_pending_order_rejects_empty_cart(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)

    with pytest.raises(ValueError, match="carrito est. vac.o"):
        VendorOrderService.create_pending_order(
            company_id=data["company_a"].id,
            conversation_id=conversation.id,
            customer_name="Cliente A",
            actor_user_id=data["user_a"].id,
        )


def test_create_pending_order_creates_quote_payment_and_mp_flow(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )

    result = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        customer_name="Cliente A",
        customer_phone="5491112345678",
        actor_user_id=data["user_a"].id,
    )

    quote = db.session.get(Quote, result["quote_id"])
    payment = Payment.query.filter_by(company_id=data["company_a"].id, preference_id="pref-test-001").one()
    checkout = next(payload for kind, payload in calls if kind == "checkout")

    assert result["success"] is True
    assert result["payment_url"] == "https://payments.test/checkout/001"
    assert quote.status == "ENVIADO"
    assert quote.client_id == data["client_a"].id
    assert quote.number == f"P-{quote.id:06d}"
    assert len(quote.items) == 1
    assert float(quote.items[0].subtotal) == 200.0
    assert payment.status == "pending"
    assert payment.payment_method == "mercadopago_ai_order"
    assert payment.external_reference.startswith(f"flow:ai_order|company_id:{data['company_a'].id}|quote_id:{quote.id}")
    assert checkout["company_id"] == data["company_a"].id
    assert checkout["conversation_id"] == conversation.id
    assert checkout["user_id"] == data["user_a"].id
    assert checkout["access_token"] == "test-access-token"
    assert conversation.metadata_json[PENDING_QUOTE_KEY] == quote.id
    assert conversation.metadata_json[PENDING_PAYMENT_KEY] == result["payment_url"]


def test_checkout_uses_discounted_unit_price_in_quote_and_mercado_pago(vendor_database, monkeypatch):
    data = vendor_database
    data["product_a"].discount = 10
    db.session.commit()
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)

    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )
    cart = VendorOrderService.get_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
    )
    assert cart["total"] == 180.0
    assert cart["items"][0]["unit_price"] == 90.0

    result = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        customer_name="Cliente A",
        actor_user_id=data["user_a"].id,
    )
    quote = db.session.get(Quote, result["quote_id"])
    checkout = next(payload for kind, payload in calls if kind == "checkout")

    assert float(quote.total_amount) == 180.0
    assert checkout["amount"] == 180.0
    product_items = [item for item in checkout["items"] if not str(item["id"]).startswith("shipping-")]
    assert product_items == [
        {
            "id": str(data["product_a"].id),
            "title": data["product_a"].name,
            "description": data["product_a"].name,
            "quantity": 2,
            "currency_id": "ARS",
            "unit_price": 90.0,
        }
    ]


def test_finalize_paid_order_clears_webchat_checkout_state(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )
    created = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )
    payment = Payment.query.filter_by(company_id=data["company_a"].id).one()
    quote = db.session.get(Quote, created["quote_id"])
    payment_data = {
        "id": "123456",
        "transaction_amount": float(quote.total_amount),
        "currency_id": "ARS",
        "external_reference": payment.external_reference,
        "payment_method_id": "account_money",
    }

    result = VendorOrderService.finalize_paid_order(
        company_id=data["company_a"].id,
        quote_id=quote.id,
        payment_data=payment_data,
    )

    db.session.refresh(conversation)
    state = conversation.metadata_json
    assert result["status"] == "converted"
    assert state.get(PENDING_QUOTE_KEY) is None
    assert state.get(PENDING_PAYMENT_KEY) is None
    assert CART_KEY not in state
    assert state[LAST_ORDER_KEY]["order_status"] == "confirmado"
    assert state[LAST_ORDER_KEY]["payment_status"] == "approved"
    assert state[LAST_ORDER_KEY]["quote_id"] == quote.id
    assert state[LAST_ORDER_KEY]["sale_id"] == result["sale_id"]
    assert data["product_a"].stock == 8


def test_mercado_pago_rejects_preference_when_lines_do_not_match_total(vendor_database):
    service = MercadoPagoService()
    with pytest.raises(ValueError, match="no coincide con el importe del pedido"):
        service.create_ai_order_checkout_preference(
            title="Pedido P-000001 - StockARmobile",
            items=[
                {
                    "id": "1",
                    "title": "Cafe clasico",
                    "description": "Cafe clasico",
                    "quantity": 2,
                    "currency_id": "ARS",
                    "unit_price": 100,
                }
            ],
            amount=180,
            currency="ARS",
            external_reference="test",
            company_id=vendor_database["company_a"].id,
            user_id=vendor_database["user_a"].id,
            quote_id=1,
            conversation_id=1,
            return_url="http://test.local/pago",
        )


def test_legacy_shipping_config_normalizes_to_fixed_and_creates_both_links(vendor_database, monkeypatch):
    data = vendor_database
    _configure_vendor_shipping(data["company_a"], mode="legacy_percent", standard_cost="50.00")
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)

    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )

    result = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        customer_name="Nuevo Comprador",
        customer_phone="5491119998888",
        delivery_method="envio",
        delivery_address="Av. Siempre Viva 123",
        delivery_city="Resistencia",
        delivery_province="Chaco",
        delivery_postal_code="3500",
        delivery_reference="Portón negro",
        delivery_notes="Entregar por la tarde",
        actor_user_id=data["user_a"].id,
    )

    quote = db.session.get(Quote, result["quote_id"])
    delivery = db.session.get(QuoteDelivery, quote.id)
    checkout = next(payload for kind, payload in calls if kind == "checkout")

    assert result["success"] is True
    assert "shipping_pending" not in result
    assert result["payment_url"] == "https://payments.test/checkout/001"
    assert result["quote_url"].startswith("http://test.local/presupuestos/publico/")
    assert quote.total_amount == 250
    assert quote.surcharge == 50
    assert quote.surcharge_type == "fixed"
    assert quote.surcharge_value == 50
    assert delivery.shipping_cost == 50
    assert delivery.shipping_status == "confirmed"
    assert delivery.shipping_source == "fixed"
    assert checkout["amount"] == 250
    assert any(item["id"] == f"shipping-{quote.id}" and item["unit_price"] == 50 for item in checkout["items"])
    assert Payment.query.filter_by(company_id=data["company_a"].id).count() == 1
    assert conversation.metadata_json[PENDING_QUOTE_KEY] == quote.id
    assert conversation.metadata_json[PENDING_PAYMENT_KEY] == result["payment_url"]

def test_pending_checkout_reserves_stock_for_other_vendor_checkouts(vendor_database, monkeypatch):
    data = vendor_database
    first = _conversation(data["company_a"].id)
    second = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)

    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=first.id,
        items=[{"product_query": "Cafe clasico", "quantity": 7}],
    )
    first_result = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=first.id,
        actor_user_id=data["user_a"].id,
    )
    assert first_result["success"] is True

    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=second.id,
        items=[{"product_query": "Cafe clasico", "quantity": 4}],
    )
    with pytest.raises(ValueError, match="Stock reservado para otro pedido"):
        VendorOrderService.create_pending_order(
            company_id=data["company_a"].id,
            conversation_id=second.id,
            actor_user_id=data["user_a"].id,
        )

    assert Payment.query.filter_by(company_id=data["company_a"].id).count() == 1


def test_retry_payment_keeps_legacy_shipping_when_snapshot_is_missing(vendor_database, monkeypatch):
    data = vendor_database
    company = data["company_a"]
    _configure_vendor_shipping(company, mode="fixed", standard_cost="50.00")
    conversation = _conversation(company.id)
    calls = []
    _mock_checkout(monkeypatch, calls)

    VendorOrderService.update_cart(
        company_id=company.id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )
    first = VendorOrderService.create_pending_order(
        company_id=company.id,
        conversation_id=conversation.id,
        delivery_method="envio",
        customer_name="Comprador legado",
        customer_phone="5491119998888",
        delivery_address="Av. Siempre Viva 123",
        delivery_city="Resistencia",
        delivery_province="Chaco",
        actor_user_id=data["user_a"].id,
    )
    quote = db.session.get(Quote, first["quote_id"])
    payment = Payment.query.filter_by(company_id=company.id).one()
    payment.status = "rejected"
    quote.charges_json = None
    db.session.commit()

    second = VendorOrderService.retry_payment(
        company_id=company.id,
        conversation_id=conversation.id,
    )

    checkout_calls = [payload for kind, payload in calls if kind == "checkout"]
    assert second["success"] is True
    assert checkout_calls[-1]["amount"] == 150.0
    assert any(
        item["title"] == "Envío a domicilio" and item["unit_price"] == 50.0
        for item in checkout_calls[-1]["items"]
    )


def test_create_pending_order_reuses_existing_pending_flow(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )

    first = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )
    payment = Payment.query.filter_by(company_id=data["company_a"].id).one()
    payment.external_reference = f"flow:ai_order|company_id:{data['company_a'].id}|quote_id:{first['quote_id']}"
    db.session.commit()
    second = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )

    assert second["success"] is True
    assert second["existing"] is True
    assert second["quote_id"] == first["quote_id"]
    assert Quote.query.filter_by(company_id=data["company_a"].id).count() == 1
    assert Payment.query.filter_by(company_id=data["company_a"].id).count() == 1
    assert [kind for kind, _ in calls].count("checkout") == 1


def test_create_pending_order_reuses_its_own_pending_payment(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )

    first = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )
    first_payment = Payment.query.filter_by(company_id=data["company_a"].id).one()
    second = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )

    assert second["success"] is True
    assert second["existing"] is True
    assert second["quote_id"] == first["quote_id"]
    assert Quote.query.filter_by(company_id=data["company_a"].id).count() == 1
    assert Payment.query.filter_by(company_id=data["company_a"].id).count() == 1
    assert Payment.query.filter_by(company_id=data["company_a"].id).one().id == first_payment.id
    assert [kind for kind, _ in calls].count("checkout") == 1


def test_cart_change_invalidates_pending_order_reuse(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )
    VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )

    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )

    assert PENDING_QUOTE_KEY not in conversation.metadata_json
    assert PENDING_PAYMENT_KEY not in conversation.metadata_json


def test_create_pending_order_propagates_mp_missing_payment_url(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls, result={"id": "pref-without-url"})
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )

    with pytest.raises(RuntimeError, match="no devolvi. un link de pago"):
        VendorOrderService.create_pending_order(
            company_id=data["company_a"].id,
            conversation_id=conversation.id,
            actor_user_id=data["user_a"].id,
        )

    assert Payment.query.filter_by(company_id=data["company_a"].id).count() == 0
    assert PENDING_QUOTE_KEY not in conversation.metadata_json
    assert PENDING_PAYMENT_KEY not in conversation.metadata_json


def test_create_pending_order_rechecks_stock_before_checkout(vendor_database, monkeypatch):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )
    data["product_a"].stock = 1
    db.session.commit()

    with pytest.raises(ValueError, match="Stock insuficiente para Cafe clasico"):
        VendorOrderService.create_pending_order(
            company_id=data["company_a"].id,
            conversation_id=conversation.id,
            actor_user_id=data["user_a"].id,
        )

    assert not calls


def test_runtime_rejects_company_id_supplied_by_model_for_vendor_tool(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)

    result = AgentRuntime._execute_tool(
        "agregar_al_carrito",
        company_id=data["company_a"].id,
        arguments={
            "product_query": "Cafe clasico",
            "quantity": 1,
            "company_id": data["company_b"].id,
        },
        context={"conversation_id": conversation.id},
    )

    assert result == {"success": False, "error": "company_id must be passed explicitly"}
    assert CART_KEY not in (conversation.metadata_json or {})


def test_checkout_charges_are_calculated_and_sent_as_separate_mp_lines(vendor_database, monkeypatch):
    data = vendor_database
    company = data["company_a"]
    _configure_vendor_charges(
        company,
        charges=[
            {"id": "iva", "name": "IVA", "type": "percentage", "value": "21", "base": "products", "active": True},
            {"id": "embalaje", "name": "Embalaje", "type": "fixed", "value": "10", "base": "products", "active": True},
        ],
    )
    _configure_vendor_shipping(company, mode="fixed", standard_cost="50.00")

    conversation = _conversation(company.id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=company.id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 2}],
    )

    result = VendorOrderService.create_pending_order(
        company_id=company.id,
        conversation_id=conversation.id,
        delivery_method="envio",
        customer_name="Comprador",
        customer_phone="5491119998888",
        delivery_address="Av. Siempre Viva 123",
        delivery_city="Resistencia",
        delivery_province="Chaco",
        actor_user_id=data["user_a"].id,
    )

    quote = db.session.get(Quote, result["quote_id"])
    delivery = db.session.get(QuoteDelivery, quote.id)
    checkout = next(payload for kind, payload in calls if kind == "checkout")
    snapshot = json.loads(quote.charges_json or "[]")

    assert result["success"] is True
    assert quote.subtotal == 200
    assert quote.surcharge == 60
    assert quote.tax == 42
    assert quote.total_amount == 302
    assert delivery.shipping_cost == 50
    assert [row["name"] for row in snapshot] == ["Envío a domicilio", "IVA", "Embalaje"]
    assert [row["amount"] for row in snapshot] == ["50.00", "42.00", "10.00"]
    assert abs(sum(float(item["quantity"]) * float(item["unit_price"]) for item in checkout["items"]) - 302.0) < 0.01
    assert any(item["title"] == "IVA" and item["unit_price"] == 42.0 for item in checkout["items"])
    assert any(item["title"] == "Embalaje" and item["unit_price"] == 10.0 for item in checkout["items"])
    assert any(item["title"] == "Envío a domicilio" and item["unit_price"] == 50.0 for item in checkout["items"])


def test_retry_payment_reuses_persisted_checkout_charges(vendor_database, monkeypatch):
    data = vendor_database
    _configure_vendor_charges(
        data["company_a"],
        charges=[{"id": "iva", "name": "IVA 21%", "type": "percentage", "value": "21", "base": "products", "active": True}],
    )
    conversation = _conversation(data["company_a"].id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )
    first = VendorOrderService.create_pending_order(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )
    payment = Payment.query.filter_by(company_id=data["company_a"].id).one()
    payment.status = "rejected"
    db.session.commit()

    second = VendorOrderService.retry_payment(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
    )
    checkout_calls = [payload for kind, payload in calls if kind == "checkout"]
    assert second["success"] is True
    assert len(checkout_calls) == 2
    retry_items = checkout_calls[1]["items"]
    assert any(item["title"] == "IVA 21%" and item["unit_price"] == 21.0 for item in retry_items)
    assert checkout_calls[1]["amount"] == 121.0
    assert second["quote_number"] == VendorOrderService._order_row(
        company_id=data["company_a"].id,
        quote=db.session.get(Quote, first["quote_id"]),
    )["quote_number"]



def test_ai_order_attention_state_is_tenant_scoped_and_visible(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    conversation.metadata_json = {
        "ai_attention": {
            "status": "human",
            "reason": "Cliente pidió hablar con una persona.",
            "requested_at": "2026-09-24T02:00:00",
        }
    }
    from app import Payment

    quote = Quote(
        company_id=data["company_a"].id,
        created_by_user_id=data["user_a"].id,
        seller_id=data["user_a"].id,
        client_id=data["client_a"].id,
        number="P-ATTN-001",
        subtotal=100,
        total_amount=100,
        status="ENVIADO",
        observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
    )
    db.session.add(quote)
    db.session.flush()
    payment = Payment(
        company_id=data["company_a"].id,
        provider="mercadopago_ai_order",
        external_reference=f"flow:ai_order|company_id:{data['company_a'].id}|quote_id:{quote.id}|conversation_id:{conversation.id}|",
        status="pending",
    )
    db.session.add(payment)
    db.session.commit()

    from whatsapp_agent import _ai_order_row

    row = _ai_order_row(data["company_a"].id, quote)
    assert row["conversation_id"] == conversation.id
    assert row["attention"]["key"] == "human"
    assert row["attention"]["label"] == "Atención humana"
    assert row["attention"]["reason"] == "Cliente pidió hablar con una persona."



def test_ai_followup_planner_pauses_when_human_attention_is_active(vendor_database):
    data = vendor_database
    conversation = _conversation(data["company_a"].id)
    conversation.external_conversation_id = "5491112345678"
    conversation.metadata_json = {
        "ai_attention": {"status": "human", "reason": "Cliente pidió atención humana."},
        "vendor_cart": {
            "items": [{"product_id": data["product_a"].id, "quantity": 1, "unit_price": 100}],
            "total": 100,
            "currency": "ARS",
            "line_count": 1,
        },
    }
    from stockarmobile.models.conversations import ConversationMessage
    from datetime import datetime

    db.session.add(
        ConversationMessage(
            company_id=conversation.company_id,
            conversation_id=conversation.id,
            sender_type="user",
            role="user",
            content="Quiero hablar con una persona",
            created_at=datetime(2026, 9, 24, 0, 0, 0),
        )
    )
    db.session.commit()

    result = AIFollowupService.scan(now=datetime(2026, 9, 24, 3, 0, 0), dry_run=True)

    assert result["queued"] == 0
    assert result["eligible"] == 0
def _configure_vendor_charges(company, *, charges):
    payload = {"ai_agent": {"vendor_options": {
        "shipping_mode": "fixed",
        "standard_shipping_cost": "0.00",
        "checkout_charges": charges,
    }}}
    company.preferences_json = json.dumps(payload, ensure_ascii=False)
    db.session.commit()


def _configure_vendor_shipping(company, *, mode, standard_cost="0.00"):
    try:
        current = json.loads(company.preferences_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        current = {}
    ai = current.get("ai_agent") if isinstance(current.get("ai_agent"), dict) else {}
    vendor_options = ai.get("vendor_options") if isinstance(ai.get("vendor_options"), dict) else {}
    vendor_options.update({
        "shipping_mode": mode,
        "standard_shipping_cost": standard_cost,
    })
    ai["vendor_options"] = vendor_options
    current["ai_agent"] = ai
    company.preferences_json = json.dumps(current, ensure_ascii=False)
    db.session.commit()











def test_vendor_quote_snapshot_is_rendered_in_public_pdf_detail_and_whatsapp(vendor_database, monkeypatch):
    data = vendor_database
    company = data["company_a"]
    _configure_vendor_charges(
        company,
        charges=[
            {"id": "iva", "name": "IVA", "type": "percentage", "value": "21", "base": "products", "active": True},
            {"id": "embalaje", "name": "Embalaje", "type": "fixed", "value": "10", "base": "products", "active": True},
        ],
    )
    conversation = _conversation(company.id)
    calls = []
    _mock_checkout(monkeypatch, calls)
    VendorOrderService.update_cart(
        company_id=company.id,
        conversation_id=conversation.id,
        items=[{"product_query": "Cafe clasico", "quantity": 1}],
    )
    created = VendorOrderService.create_pending_order(
        company_id=company.id,
        conversation_id=conversation.id,
        actor_user_id=data["user_a"].id,
    )
    quote = db.session.get(Quote, created["quote_id"])

    rows = _quote_charge_display_rows(quote)
    assert [row["label"] for row in rows] == ["IVA 21%", "Embalaje"]
    assert [float(row["amount"]) for row in rows] == [21.0, 10.0]

    whatsapp = _build_quote_whatsapp_message(quote, company)
    assert "IVA 21%: ARS 21.00" in whatsapp
    assert "Embalaje: ARS 10.00" in whatsapp
    assert "Impuestos:" not in whatsapp

    client = stock_app.app.test_client()
    public = client.get(_build_public_quote_url(quote.id))
    assert public.status_code == 200
    html = public.get_data(as_text=True)
    assert "IVA 21%" in html
    assert "$21.00" in html
    assert "Embalaje" in html
    assert "Impuestos" not in html

    pdf = client.get(_build_public_quote_pdf_url(quote.id))
    assert pdf.status_code == 200
    assert pdf.content_type.startswith("application/pdf")
    assert len(pdf.data) > 0


def _create_manual_quote_for_public_acceptance(data):
    quote = Quote(
        company_id=data["company_a"].id,
        created_by_user_id=data["user_a"].id,
        seller_id=data["user_a"].id,
        client_id=data["client_a"].id,
        number="P-MANUAL-001",
        subtotal=100,
        discount=0,
        surcharge=0,
        tax=21,
        total_amount=121,
        status="ENVIADO",
    )
    db.session.add(quote)
    db.session.flush()
    from app import QuoteItem
    db.session.add(
        QuoteItem(
            quote_id=quote.id,
            product_id=data["product_a"].id,
            description=data["product_a"].name,
            quantity=1,
            unit_price=100,
            discount=0,
            subtotal=100,
            sort_order=1,
        )
    )
    db.session.commit()
    return quote


def test_public_quote_acceptance_get_is_side_effect_free(vendor_database):
    data = vendor_database
    quote = _create_manual_quote_for_public_acceptance(data)

    client = stock_app.app.test_client()
    response = client.get(_build_public_quote_accept_url(quote.id), follow_redirects=False)

    assert response.status_code in (301, 302)
    db.session.refresh(quote)
    assert quote.status == "ENVIADO"
    assert quote.converted_sale_id is None
    from app import Sale
    assert Sale.query.filter_by(company_id=data["company_a"].id).count() == 0


def test_manual_quote_acceptance_requires_post_and_does_not_create_sale(vendor_database):
    data = vendor_database
    quote = _create_manual_quote_for_public_acceptance(data)

    client = stock_app.app.test_client()
    response = client.post(_build_public_quote_accept_url(quote.id), follow_redirects=False)

    assert response.status_code in (301, 302)
    db.session.refresh(quote)
    assert quote.status == "APROBADO"
    assert quote.converted_sale_id is None
    from app import Sale
    assert Sale.query.filter_by(company_id=data["company_a"].id).count() == 0
