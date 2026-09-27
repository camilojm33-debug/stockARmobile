from io import BytesIO

from openpyxl import Workbook

from services.saas_commercial_service import (
    build_campaign_recipients,
    lead_score,
    normalize_email,
    normalize_phone,
    parse_consent,
    parse_prospect_file,
)


def test_normalize_email_and_phone():
    assert normalize_email(" VENTAS@EMPRESA.COM ") == "ventas@empresa.com"
    assert normalize_email("not-an-email") is None
    assert normalize_phone("+54 9 11-1234-5678") == "5491112345678"


def test_parse_consent_values():
    assert parse_consent("sí") == "opted_in"
    assert parse_consent("no") == "opted_out"
    assert parse_consent("quizas") == "unknown"


def test_target_lead_score():
    row = {
        "industry": "Ferretería industrial",
        "email": "ventas@e.com",
        "phone": "5491112345678",
        "website": "https://tienda.e.com",
        "province": "Buenos Aires",
        "locality": "Morón",
        "instagram": "",
        "facebook": "",
    }
    assert lead_score(row) >= 80


def test_parse_csv():
    payload = """Comercio;Rubro;Localidad;Provincia;Email;WhatsApp;Web;Consentimiento Email
Ferretería Uno;Ferretería;Morón;Buenos Aires;ventas@uno.com;+54 9 11 5555-1111;https://uno.com;si
Fila incompleta;;;;;;;si
""".encode("utf-8")

    result = parse_prospect_file(payload, "prospectos.csv")
    assert result["format"] == "csv"
    assert len(result["rows"]) == 1
    assert result["rows"][0]["email_consent_status"] == "opted_in"
    assert result["invalid_count"] == 1


def test_parse_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.append(["Comercio", "Rubro", "Email", "Localidad", "Provincia"])
    ws.append(["Pet Shop Demo", "Pet Shop", "hola@petshop.com", "CABA", "CABA"])
    buf = BytesIO()
    wb.save(buf)

    result = parse_prospect_file(buf.getvalue(), "prospectos.xlsx")
    assert result["format"] == "xlsx"
    assert result["rows"][0]["company_name"] == "Pet Shop Demo"


def test_parse_rejects_unsupported():
    try:
        parse_prospect_file(b"", "prospectos.pdf")
    except ValueError as exc:
        assert "CSV" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_campaign_recipient_selection_requires_email_opt_in(app):
    from app import SaaSCampaign, SaaSLead, SaaSLeadConsent, User, db

    with app.app_context():
        user = User(username="commercial-admin", email="commercial-admin@example.com", role="superadmin", active=True)
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        opted = SaaSLead(
            company_name="Optado",
            contact_name="Contacto",
            email="optado@example.com",
            email_status="valid",
            email_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        unknown = SaaSLead(
            company_name="Desconocido",
            contact_name="Contacto",
            email="unknown@example.com",
            email_status="valid",
            email_consent_status="unknown",
            created_by_user_id=user.id,
        )
        db.session.add_all([opted, unknown])
        db.session.flush()
        db.session.add_all([
            SaaSLeadConsent(lead_id=opted.id, unsubscribe_token="token-opted"),
            SaaSLeadConsent(lead_id=unknown.id, unsubscribe_token="token-unknown"),
        ])

        campaign = SaaSCampaign(
            name="Prueba",
            subject="Hola",
            channel="email",
            body_html="<p>Hola {{contacto}}</p>",
            body_text="Hola {{contacto}}",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        summary = build_campaign_recipients(db.session, campaign.id)
        assert summary["eligible"] == 1
        assert len(campaign.recipients) == 1
        assert campaign.recipients[0].lead_id == opted.id
