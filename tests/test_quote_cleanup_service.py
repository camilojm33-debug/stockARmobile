from datetime import timedelta
from uuid import uuid4

import pytest

from app import Company, Payment, Quote, User, db, utcnow
from services.quote_cleanup_service import AI_ORDER_MARKER, QuoteCleanupService


@pytest.fixture
def cleanup_database(app):
    company = Company(name="Empresa limpieza", active=True)
    other = Company(name="Otra empresa", active=True)
    db.session.add_all([company, other])
    db.session.flush()

    admin = User(
        username="cleanup_admin",
        email="cleanup@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(admin)
    db.session.flush()
    db.session.commit()
    return {"company": company, "other": other, "admin": admin}


def make_quote(company_id, user_id, *, status="BORRADOR", days_old=120, ai=False, converted_sale_id=None):
    return Quote(
        company_id=company_id,
        created_by_user_id=user_id,
        seller_id=user_id,
        number=f"TEST-{uuid4().hex[:16]}",
        status=status,
        date=utcnow() - timedelta(days=days_old),
        updated_at=utcnow() - timedelta(days=days_old),
        total_amount=1000,
        observations=AI_ORDER_MARKER if ai else "Presupuesto manual",
        converted_sale_id=converted_sale_id,
    )


def test_cleanup_preview_is_tenant_scoped_and_conservative(cleanup_database):
    company = cleanup_database["company"]
    other = cleanup_database["other"]
    admin = cleanup_database["admin"]

    manual = make_quote(company.id, admin.id, status="VENCIDO")
    ai_free = make_quote(company.id, admin.id, status="PENDIENTE", ai=True)
    ai_paid = make_quote(company.id, admin.id, status="PENDIENTE", ai=True)
    recent = make_quote(company.id, admin.id, status="BORRADOR", days_old=10)
    converted = make_quote(company.id, admin.id, status="CONVERTIDO", converted_sale_id=999)
    other_quote = make_quote(other.id, admin.id, status="VENCIDO")
    db.session.add_all([manual, ai_free, ai_paid, recent, converted, other_quote])
    db.session.flush()

    payment = Payment(
        payment_id="mp-cleanup-1",
        external_reference=f"flow:ai_order|company_id:{company.id}|quote_id:{ai_paid.id}|conversation_id:99",
        company_id=company.id,
        amount=1000,
        currency="ARS",
        status="pending",
        payment_method="mercadopago_ai_order",
        provider="mercadopago_ai_order",
    )
    db.session.add(payment)
    db.session.commit()

    preview = QuoteCleanupService.preview(company_id=company.id, older_than_days=90, kind="all")

    ids = {row["id"] for row in preview["eligible"]}
    protected = {row["id"]: row["protected_reason"] for row in preview["protected"]}

    assert manual.id in ids
    assert ai_free.id in ids
    assert ai_paid.id not in ids
    assert protected[ai_paid.id] == "pago_activo"
    assert converted.id not in ids
    assert protected[converted.id] == "convertido_a_venta"
    assert recent.id not in ids
    assert other_quote.id not in ids


def test_cleanup_deletes_only_eligible_rows(cleanup_database):
    company = cleanup_database["company"]
    admin = cleanup_database["admin"]

    manual = make_quote(company.id, admin.id, status="ANULADO")
    ai = make_quote(company.id, admin.id, status="RECHAZADO", ai=True)
    protected = make_quote(company.id, admin.id, status="PENDIENTE", ai=True)
    db.session.add_all([manual, ai, protected])
    db.session.flush()

    db.session.add(
        Payment(
            payment_id="mp-cleanup-2",
            external_reference=f"flow:ai_order|company_id:{company.id}|quote_id:{protected.id}|conversation_id:100",
            company_id=company.id,
            amount=1000,
            currency="ARS",
            status="approved",
            payment_method="mercadopago_ai_order",
            provider="mercadopago_ai_order",
        )
    )
    db.session.commit()

    result = QuoteCleanupService.delete_eligible(
        company_id=company.id,
        older_than_days=90,
        kind="all",
        ip_address="127.0.0.1",
    )

    assert result["deleted_count"] == 2
    assert db.session.get(Quote, manual.id) is None
    assert db.session.get(Quote, ai.id) is None
    assert db.session.get(Quote, protected.id) is not None


def test_cleanup_page_and_confirmation_are_admin_only(app, cleanup_database):
    company = cleanup_database["company"]
    admin = cleanup_database["admin"]

    old_quote = make_quote(company.id, admin.id, status="VENCIDO")
    db.session.add(old_quote)
    db.session.commit()

    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session["_user_id"] = str(admin.id)
        session["_fresh"] = True

    response = test_client.get("/presupuestos/limpieza?days=90&kind=all")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Limpieza de presupuestos" in html
    assert "BORRAR" in html

    rejected = test_client.post(
        "/presupuestos/limpieza/eliminar",
        data={"days": "90", "kind": "all", "confirmation": "NO"},
        follow_redirects=True,
    )
    assert rejected.status_code == 200
    assert db.session.get(Quote, old_quote.id) is not None
