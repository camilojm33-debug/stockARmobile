from datetime import timedelta
from uuid import uuid4

import pytest

from app import AuditLog, Company, Payment, Quote, User, db, utcnow
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


def test_cleanup_origin_filter_is_applied_before_limit(cleanup_database):
    company = cleanup_database["company"]
    other = cleanup_database["other"]
    admin = cleanup_database["admin"]

    # The matching AI quote is older than 90 days, but older manual quotes would
    # consume a pre-filter LIMIT and make the AI-only preview incorrectly empty.
    old_manual = [
        make_quote(company.id, admin.id, status="VENCIDO", days_old=600 - index * 10)
        for index in range(4)
    ]
    ai_target = make_quote(company.id, admin.id, status="VENCIDO", days_old=200, ai=True)

    # Check the inverse direction in a second tenant: old AI quotes must not
    # hide an eligible manual quote from a manual-only preview.
    other_admin = User(
        username="cleanup_other_admin",
        email="cleanup-other@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=other.id,
    )
    db.session.add(other_admin)
    db.session.flush()
    old_ai = [
        make_quote(other.id, other_admin.id, status="VENCIDO", days_old=600 - index * 10, ai=True)
        for index in range(4)
    ]
    manual_target = make_quote(other.id, other_admin.id, status="VENCIDO", days_old=200)

    db.session.add_all([*old_manual, ai_target, *old_ai, manual_target])
    db.session.commit()

    ai_preview = QuoteCleanupService.preview(
        company_id=company.id, older_than_days=90, kind="ai", limit=3
    )
    manual_preview = QuoteCleanupService.preview(
        company_id=other.id, older_than_days=90, kind="manual", limit=3
    )

    assert {row["id"] for row in ai_preview["eligible"]} == {ai_target.id}
    assert ai_preview["manual_count"] == 0
    assert ai_preview["ai_count"] == 1
    assert {row["id"] for row in manual_preview["eligible"]} == {manual_target.id}
    assert manual_preview["ai_count"] == 0
    assert manual_preview["manual_count"] == 1


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
    assert not AuditLog.query.filter_by(
        company_id=company.id,
        action="quote_cleanup_delete",
        entity="quote",
        entity_id=old_quote.id,
    ).first()

    deleted = test_client.post(
        "/presupuestos/limpieza/eliminar",
        data={"days": "90", "kind": "all", "confirmation": "BORRAR"},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert "Se eliminaron 1 presupuestos." in deleted.get_data(as_text=True)
    assert db.session.get(Quote, old_quote.id) is None

    audit = AuditLog.query.filter_by(
        company_id=company.id,
        action="quote_cleanup_delete",
        entity="quote",
        entity_id=old_quote.id,
    ).first()
    assert audit is not None
    assert "antiguedad>90d" in audit.detail
