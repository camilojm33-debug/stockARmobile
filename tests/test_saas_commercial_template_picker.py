from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from services import saas_commercial_templates as templates_service
from services.saas_commercial_templates import commercial_campaign_template_compatibility


ROOT = Path(__file__).resolve().parents[1]


def _template(*, body="Hola {{1}}", status="APPROVED", category="MARKETING", extra=None):
    components = [{"type": "BODY", "text": body}]
    if extra:
        components.extend(extra)
    return {
        "name": "stockarmobile_presentacion",
        "language": "es_AR",
        "status": status,
        "category": category,
        "components": components,
    }


def test_commercial_template_compatibility_supports_positional_body_variables():
    one = commercial_campaign_template_compatibility(_template(body="Hola {{1}}"))
    many = commercial_campaign_template_compatibility(_template(body="Hola {{1}}, {{2}} y {{3}}"))

    assert one["campaign_compatible"] is True
    assert one["variable_count"] == 1
    assert many["campaign_compatible"] is True
    assert many["variable_count"] == 3


def test_commercial_template_compatibility_supports_static_body_without_variables():
    result = commercial_campaign_template_compatibility(_template(body="Conocé StockArMobile y organizá tu negocio."))

    assert result["campaign_compatible"] is True
    assert result["variable_count"] == 0


def test_commercial_template_compatibility_rejects_nonconsecutive_or_named_variables():
    skipped = commercial_campaign_template_compatibility(_template(body="Hola {{1}} {{3}}"))
    named = commercial_campaign_template_compatibility(_template(body="Hola {{contacto}}"))

    assert skipped["campaign_compatible"] is False
    assert "consecutivas" in skipped["compatibility_reason"]
    assert named["campaign_compatible"] is False
    assert "posicionales" in named["compatibility_reason"]


def test_commercial_template_compatibility_rejects_wrong_category_and_dynamic_header():
    utility = commercial_campaign_template_compatibility(_template(category="UTILITY"))
    dynamic_header = commercial_campaign_template_compatibility(
        _template(extra=[{"type": "HEADER", "format": "TEXT", "text": "Hola {{1}}"}])
    )

    assert utility["campaign_compatible"] is False
    assert "MARKETING" in utility["compatibility_reason"]
    assert dynamic_header["campaign_compatible"] is False
    assert "fuera del cuerpo" in dynamic_header["compatibility_reason"]


def test_commercial_template_catalog_marks_supported_and_unsupported_templates(monkeypatch):
    # Set unique names so the order is deterministic.
    raw = [
        _template(body="Hola {{1}} {{2}}"),
        {**_template(body="Hola {{1}} {{3}}"), "name": "other"},
    ]
    monkeypatch.setattr(templates_service, "_list_waba_templates", lambda waba_id, token: raw)

    listed = templates_service.list_commercial_campaign_templates({
        "business_account_id": "12345",
        "access_token": "secret",
    })

    assert listed[0]["name"] == "stockarmobile_presentacion"
    assert listed[0]["campaign_compatible"] is True
    assert listed[0]["variable_count"] == 2
    assert listed[1]["name"] == "other"
    assert listed[1]["campaign_compatible"] is False


def test_commercial_campaign_parameter_builder_supplies_product_and_campaign_text():
    from services.saas_commercial_service import _whatsapp_template_parameters

    flask_app = Flask(__name__)
    flask_app.config["APP_URL"] = "https://app.stockarmobile.com"
    campaign = SimpleNamespace(
        whatsapp_parameter_fields="contacto,producto,mensaje,trial_url",
        body_text="Hola {{contacto}}: gestioná stock y ventas desde un solo lugar.",
        subject="Conocé StockArMobile",
    )
    lead = SimpleNamespace(
        company_name="Comercio Demo",
        contact_name="Carla",
        industry="Ferretería",
        locality="Resistencia",
    )

    with flask_app.app_context():
        result = _whatsapp_template_parameters(campaign, lead)

    assert result == [
        "Carla",
        "StockArMobile",
        "Hola Carla: gestioná stock y ventas desde un solo lugar.",
        "https://app.stockarmobile.com/auth/register?selected_plan=trial",
    ]


def test_superadmin_campaign_forms_load_meta_templates_and_map_each_variable():
    create_form = (ROOT / "templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    edit_form = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    picker_js = (ROOT / "static/assets/js/superadmin-campaign-template-picker.js").read_text(encoding="utf-8")
    route_source = (ROOT / "saas.py").read_text(encoding="utf-8")

    for template in (create_form, edit_form):
        assert "js-whatsapp-template-form" in template
        assert "saas.whatsapp_commercial_templates" in template
        assert "data-whatsapp-template-picker" in template
        assert 'name="whatsapp_template_name"' in template
        assert 'name="whatsapp_template_language"' in template
        assert 'name="whatsapp_parameter_fields"' in template
        assert "data-default-fields=" in template

    assert "data-whatsapp-variable" in picker_js
    assert "form.dataset.defaultFields" in picker_js
    assert "campaign_compatible" in picker_js
    assert "data-current-name" in edit_form
    assert "def whatsapp_commercial_templates():" in route_source
    assert "@superadmin_required\ndef whatsapp_commercial_templates():" in route_source


def test_campaign_preflight_checks_the_number_of_whatsapp_parameters():
    source = (ROOT / "services/saas_campaign_preflight.py").read_text(encoding="utf-8")

    assert "expected_parameters = int(validation.get(\"variable_count\") or 0)" in source
    assert "len(parameter_fields) == expected_parameters" in source
    assert "COMMERCIAL_WHATSAPP_PARAMETER_FIELDS" in source



def test_campaign_preflight_blocks_a_wrong_whatsapp_parameter_count(app, monkeypatch):
    from services.saas_campaign_preflight import campaign_preflight

    campaign = SimpleNamespace(
        name="Campaña de prueba",
        subject="Conocé StockArMobile",
        body_html="<p>Hola</p>",
        body_text="Hola",
        channel="whatsapp",
        target_count=1,
        whatsapp_template_name="stockarmobile_presentacion",
        whatsapp_template_language="es_AR",
        whatsapp_parameter_fields="contacto,producto",
        segment_json="{}",
    )

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        monkeypatch.setattr(
            "services.saas_commercial_service.campaign_audience_metrics",
            lambda db_session, current_campaign: {"eligible_whatsapp": 1},
        )
        monkeypatch.setattr(
            "services.saas_commercial_service.crm_campaign_dispatch_worker_started",
            lambda: True,
        )
        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.commercial_whatsapp_summary",
            lambda: {"connected": True, "display_phone_number": "+54 9 11 5555-1234"},
        )
        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.validate_commercial_whatsapp_template",
            lambda *args, **kwargs: {
                "ok": True,
                "status": "APPROVED",
                "category": "MARKETING",
                "variable_count": 3,
                "detail": "Plantilla aprobada.",
            },
        )

        report = campaign_preflight(None, campaign)

    assert report["ready"] is False
    whatsapp_check = next(check for check in report["checks"] if check["code"] == "whatsapp_config")
    assert whatsapp_check["ok"] is False
    assert "requiere 3 parámetro(s)" in whatsapp_check["detail"]
    assert "tiene 2" in whatsapp_check["detail"]



def test_commercial_campaign_parameter_field_defaults_are_consistent():
    from services.saas_commercial_service import get_commercial_whatsapp_parameter_fields

    from services.ai_agent.whatsapp_master_template_service import MASTER_TEMPLATE_PARAMETER_FIELDS

    assert get_commercial_whatsapp_parameter_fields(SimpleNamespace(whatsapp_parameter_fields="")) == list(
        MASTER_TEMPLATE_PARAMETER_FIELDS
    )
    assert get_commercial_whatsapp_parameter_fields(SimpleNamespace(whatsapp_parameter_fields="none")) == []
