import re

from app import SaaSLead, SaaSLeadConsent, User, db


def _create_lead_with_consent(token, company_name):
    suffix = token.replace("-", "_")
    user = User(
        username=f"unsubscribe_{suffix}",
        email=f"unsubscribe_{suffix}@example.test",
        role="superadmin",
        active=True,
    )
    user.set_password("test-password")
    db.session.add(user)
    db.session.flush()

    lead = SaaSLead(
        company_name=company_name,
        contact_name=f"Contacto {company_name}",
        email=f"{suffix}@example.test",
        email_status="valid",
        email_consent_status="opted_in",
        created_by_user_id=user.id,
    )
    db.session.add(lead)
    db.session.flush()
    consent = SaaSLeadConsent(
        lead_id=lead.id,
        unsubscribe_token=token,
        email_status="opted_in",
    )
    db.session.add(consent)
    db.session.commit()
    return lead.id, consent.id


def test_unsubscribe_get_only_renders_csrf_protected_confirmation(app):
    app.config["WTF_CSRF_ENABLED"] = True
    lead_id, consent_id = _create_lead_with_consent("get-token", "Comercio GET")
    response = app.test_client().get("/superadmin/crm/unsubscribe/get-token")

    assert response.status_code == 200
    assert b"Confirmar baja" in response.data
    assert b'name="csrf_token"' in response.data

    db.session.expire_all()
    lead = db.session.get(SaaSLead, lead_id)
    consent = db.session.get(SaaSLeadConsent, consent_id)
    assert lead.email_consent_status == "opted_in"
    assert consent.email_status == "opted_in"
    assert consent.revoked_at is None


def test_human_unsubscribe_post_requires_csrf_and_explicit_confirmation(app):
    app.config["WTF_CSRF_ENABLED"] = True
    lead_id, consent_id = _create_lead_with_consent("human-token", "Comercio humano")
    client = app.test_client()

    page = client.get("/superadmin/crm/unsubscribe/human-token")
    assert page.status_code == 200
    csrf_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert csrf_match, "La página de confirmación debe incluir el token CSRF"

    rejected = client.post(
        "/superadmin/crm/unsubscribe/human-token",
        data={"confirm_unsubscribe": "1"},
    )
    assert rejected.status_code == 400

    db.session.expire_all()
    assert db.session.get(SaaSLead, lead_id).email_consent_status == "opted_in"
    assert db.session.get(SaaSLeadConsent, consent_id).email_status == "opted_in"

    accepted = client.post(
        "/superadmin/crm/unsubscribe/human-token",
        data={
            "confirm_unsubscribe": "1",
            "csrf_token": csrf_match.group(1).decode("ascii"),
        },
    )
    assert accepted.status_code == 200
    assert b"Baja realizada" in accepted.data

    db.session.expire_all()
    assert db.session.get(SaaSLead, lead_id).email_consent_status == "opted_out"
    assert db.session.get(SaaSLeadConsent, consent_id).email_status == "opted_out"
    assert db.session.get(SaaSLeadConsent, consent_id).revoked_at is not None


def test_rfc8058_one_click_is_csrf_exempt_validated_and_idempotent(app):
    app.config["WTF_CSRF_ENABLED"] = True
    lead_a, consent_a = _create_lead_with_consent("one-click-token", "Comercio A")
    lead_b, consent_b = _create_lead_with_consent("other-token", "Comercio B")
    client = app.test_client()  # No login session or CSRF cookie is established.

    response = client.post(
        "/superadmin/crm/unsubscribe-one-click/one-click-token",
        data={"List-Unsubscribe": "One-Click"},
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code == 204

    db.session.expire_all()
    assert db.session.get(SaaSLead, lead_a).email_consent_status == "opted_out"
    assert db.session.get(SaaSLeadConsent, consent_a).email_status == "opted_out"
    assert db.session.get(SaaSLead, lead_b).email_consent_status == "opted_in"
    assert db.session.get(SaaSLeadConsent, consent_b).email_status == "opted_in"

    revoked_at = db.session.get(SaaSLeadConsent, consent_a).revoked_at
    repeated = client.post(
        "/superadmin/crm/unsubscribe-one-click/one-click-token",
        data={"List-Unsubscribe": "One-Click"},
        content_type="application/x-www-form-urlencoded",
    )
    assert repeated.status_code == 204
    db.session.expire_all()
    assert db.session.get(SaaSLeadConsent, consent_a).revoked_at == revoked_at


def test_one_click_rejects_invalid_payload_method_and_token(app):
    app.config["WTF_CSRF_ENABLED"] = True
    lead_id, consent_id = _create_lead_with_consent("strict-token", "Comercio estricto")
    client = app.test_client()
    base = "/superadmin/crm/unsubscribe-one-click/strict-token"

    bad_content_type = client.post(base, json={"List-Unsubscribe": "One-Click"})
    assert bad_content_type.status_code == 400

    bad_value = client.post(
        base,
        data={"List-Unsubscribe": "wrong"},
        content_type="application/x-www-form-urlencoded",
    )
    assert bad_value.status_code == 400

    extra_field = client.post(
        base,
        data={"List-Unsubscribe": "One-Click", "unexpected": "1"},
        content_type="application/x-www-form-urlencoded",
    )
    assert extra_field.status_code == 400

    get_response = client.get(base)
    assert get_response.status_code == 405

    invalid_token = client.post(
        "/superadmin/crm/unsubscribe-one-click/not-a-token",
        data={"List-Unsubscribe": "One-Click"},
        content_type="application/x-www-form-urlencoded",
    )
    assert invalid_token.status_code == 404

    db.session.expire_all()
    assert db.session.get(SaaSLead, lead_id).email_consent_status == "opted_in"
    assert db.session.get(SaaSLeadConsent, consent_id).email_status == "opted_in"


def test_human_and_one_click_unsubscribe_urls_are_distinct(app):
    from services.saas_commercial_service import _one_click_unsubscribe_url, _unsubscribe_url

    _lead_id, _consent_id = _create_lead_with_consent("url-token", "Comercio URLs")
    lead = SaaSLead.query.filter_by(company_name="Comercio URLs").one()
    app.config["APP_URL"] = "https://www.stockarmobile.com"

    assert _unsubscribe_url(lead) == (
        "https://www.stockarmobile.com/superadmin/crm/unsubscribe/url-token"
    )
    assert _one_click_unsubscribe_url(lead) == (
        "https://www.stockarmobile.com/superadmin/crm/unsubscribe-one-click/url-token"
    )
