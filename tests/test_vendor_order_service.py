import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest

import app as stock_app
from app import Client, Company, Payment, Product, Quote, User, db
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.vendor_order_service import (
    CART_KEY,
    PENDING_PAYMENT_KEY,
    PENDING_QUOTE_KEY,
    VendorOrderService,
)
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
