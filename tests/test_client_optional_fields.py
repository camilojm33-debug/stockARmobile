import json
from datetime import timedelta
from pathlib import Path

from flask import url_for

from app import Client, Company, User, db, utcnow


def test_edit_client_with_missing_email_renders_empty_and_saves_other_fields(app):
    with app.app_context():
        company = Company(
            name="Empresa QA clientes opcionales",
            active=True,
            trial_ends_at=utcnow() + timedelta(days=10),
            preferences_json=json.dumps({"quotes_module_enabled": True}),
        )
        db.session.add(company)
        db.session.flush()

        user = User(
            username="qa_client_optional_admin",
            email="qa-client-optional@example.test",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("test-password-123")
        client_record = Client(
            company_id=company.id,
            name="Cliente sin correo",
            active=True,
            email=None,
            phone=None,
            whatsapp=None,
            address=None,
            city=None,
            notes=None,
            observations=None,
        )
        db.session.add_all([user, client_record])
        db.session.commit()
        client_id = client_record.id

        with app.test_request_context():
            edit_url = url_for("clients.edit", id=client_id)

    browser = app.test_client()
    login = browser.post(
        "/auth/login",
        data={"username": "qa_client_optional_admin", "password": "test-password-123"},
    )
    assert login.status_code in (301, 302)

    page = browser.get(edit_url)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'name="email" type="email" value=""' in html
    assert 'name="phone" value=""' in html
    assert 'name="whatsapp" value=""' in html
    assert 'name="city" value=""' in html
    assert 'name="address" value=""' in html
    assert 'name="notes" rows="3"></textarea>' in html
    assert 'name="observations" rows="3"></textarea>' in html
    assert 'name="email" type="email" value="None"' not in html

    saved = browser.post(
        edit_url,
        data={
            "name": "Cliente actualizado sin correo",
            "phone": "",
            "whatsapp": "",
            "birthday": "",
            "balance": "0",
            "credit_limit": "0",
            "address": "",
            "city": "",
            "notes": "",
            "observations": "",
            "email_marketing_consent": "unknown",
            "whatsapp_marketing_consent": "unknown",
        },
    )
    assert saved.status_code in (301, 302)

    with app.app_context():
        reloaded = db.session.get(Client, client_id)
        assert reloaded is not None
        assert reloaded.name == "Cliente actualizado sin correo"
        assert reloaded.email is None
        assert reloaded.phone is None
        assert reloaded.whatsapp is None
        assert reloaded.city is None
        assert reloaded.address is None
        assert reloaded.notes is None
        assert reloaded.observations is None


def test_product_add_button_uses_native_accessible_toggle_not_bootstrap_collapse():
    template = Path("templates/productos/index.html").read_text(encoding="utf-8-sig")
    assert "data-product-form-toggle" in template
    assert 'aria-controls="productForm"' in template
    assert "aria-expanded=" in template
    assert "productFormToggle.addEventListener('click'" in template
    assert "productForm.classList.toggle('show')" in template
    assert 'data-bs-toggle="collapse" data-bs-target="#productForm"' not in template
