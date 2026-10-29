"""Regression tests for tenant-branded AI quote observations."""

from datetime import timedelta

import quotes as quotes_module
from app import Company, Quote, User, db, utcnow
from quotes import AI_VENDOR_ORDER_OBSERVATION, _quote_observations_for_display


def test_ai_order_observation_uses_contracting_business_name_without_changing_storage():
    quote = type("QuoteStub", (), {"observations": AI_VENDOR_ORDER_OBSERVATION})()
    company = type("CompanyStub", (), {"name": "Almacen Central"})()

    assert _quote_observations_for_display(quote, company=company) == (
        "Pedido generado por el Vendedor 24 hs de Almacen Central."
    )
    assert quote.observations == AI_VENDOR_ORDER_OBSERVATION


def test_custom_observations_and_missing_business_name_keep_existing_behavior():
    company = type("CompanyStub", (), {"name": "Almacen Central"})()
    custom_quote = type("QuoteStub", (), {"observations": "Entregar el viernes."})()
    marker_quote = type("QuoteStub", (), {"observations": AI_VENDOR_ORDER_OBSERVATION})()

    assert _quote_observations_for_display(custom_quote, company=company) == "Entregar el viernes."
    assert _quote_observations_for_display(marker_quote, company=None) == AI_VENDOR_ORDER_OBSERVATION
    assert _quote_observations_for_display(type("QuoteStub", (), {"observations": None})()) == ""


def _create_ai_quote():
    company = Company(name="Almacen Central", active=True)
    db.session.add(company)
    db.session.flush()
    user = User(
        username="quote_branding_admin",
        email="quote-branding@example.test",
        password_hash="test-password-hash",
        role="admin",
        active=True,
        first_name="QA",
        last_name="Admin",
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    quote = Quote(
        number="P-OBS-BRAND-001",
        company_id=company.id,
        created_by_user_id=user.id,
        seller_id=user.id,
        observations=AI_VENDOR_ORDER_OBSERVATION,
        status="ENVIADO",
        date=utcnow(),
        expires_at=utcnow() + timedelta(days=1),
        subtotal=100,
        discount=0,
        surcharge=0,
        tax=0,
        total_amount=100,
        currency="ARS",
    )
    db.session.add(quote)
    db.session.commit()
    return quote.id


def _public_token(quote_id):
    return quotes_module._public_share_serializer().dumps(
        {"quote_id": int(quote_id)},
        salt=quotes_module.QUOTE_PUBLIC_TOKEN_SALT,
    )


def test_public_quote_page_shows_tenant_name_and_preserves_internal_marker(app):
    with app.app_context():
        quote_id = _create_ai_quote()
        token = _public_token(quote_id)
    response = app.test_client().get(f"/presupuestos/publico/{token}")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Pedido generado por el Vendedor 24 hs de Almacen Central." in page
    assert AI_VENDOR_ORDER_OBSERVATION not in page
    with app.app_context():
        quote = db.session.get(Quote, quote_id)
        assert quote.observations == AI_VENDOR_ORDER_OBSERVATION


def test_public_quote_pdf_shows_tenant_name(app, monkeypatch):
    # Disable page-stream compression in this test only so the generated PDF
    # text can be asserted without adding a PDF parsing dependency.
    original_canvas = quotes_module.canvas.Canvas

    def uncompressed_canvas(buffer, pagesize):
        return original_canvas(buffer, pagesize=pagesize, pageCompression=0)

    monkeypatch.setattr(quotes_module.canvas, "Canvas", uncompressed_canvas)
    with app.app_context():
        quote_id = _create_ai_quote()
        token = _public_token(quote_id)
    response = app.test_client().get(f"/presupuestos/publico/{token}/pdf")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert b"Pedido generado por el Vendedor 24 hs de Almacen Central." in response.data
    assert AI_VENDOR_ORDER_OBSERVATION.encode("ascii") not in response.data
    with app.app_context():
        quote = db.session.get(Quote, quote_id)
        assert quote.observations == AI_VENDOR_ORDER_OBSERVATION
