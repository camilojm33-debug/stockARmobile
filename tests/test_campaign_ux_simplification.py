from pathlib import Path
import re

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_campaign_templates_have_valid_jinja_syntax():
    env = Environment(autoescape=True)
    for path in (
        "templates/ai_agents/campaign_wizard.html",
        "templates/ai_agents/campaign_detail.html",
        "templates/saas/crm_campaigns.html",
        "templates/saas/crm_campaign_detail.html",
    ):
        env.parse(_read(path))


def test_campaign_creation_is_started_and_written_by_marketing_agent():
    route_source = _read("ai_agents.py")
    campaigns = _read("templates/ai_agents/campaigns.html")
    marketing_home = _read("templates/ai_agents/index.html")
    route_start = route_source.index("def campaign_wizard():")
    route_end = route_source.index('@bp.post("/campanas/audiencia-preview")', route_start)
    route = route_source[route_start:route_end]

    assert 'url_for("ai_agents.agent", agent="marketing", crear_campana="1")' in route
    assert "Crear campaña con Marketing IA" in campaigns
    assert "Prepará una campaña comercial para mi negocio" in marketing_home
    assert "Revisar borrador de campaña" in marketing_home
    assert "data-marketing-prompt" in marketing_home
    assert "message" in _read("services/ai_agent/tools/analyst_marketing.py")

def test_merchant_campaign_review_uses_plain_language():
    detail = _read("templates/ai_agents/campaign_detail.html")

    assert "WhatsApp está listo para esta campaña." in detail
    assert "Revisión y aprobación" in detail
    assert "Continuar a revisión" in detail
    assert "Aprobar y encolar envío" in detail
    assert "{{ campaign_template.name }}" not in detail
    assert "Plantilla validada al aprobar:" not in detail
    assert "plantilla Meta" not in detail.lower()


def test_superadmin_create_and_edit_forms_hide_technical_whatsapp_setup():
    create_form = _read("templates/saas/crm_campaigns.html")
    edit_form = _read("templates/saas/crm_campaign_detail.html")

    for form in (create_form, edit_form):
        assert "js-whatsapp-template-form" in form
        assert "data-template-list-url=" in form
        assert "data-whatsapp-template-picker" in form
        assert 'name="whatsapp_template_name"' in form
        assert 'name="whatsapp_template_language"' in form
        assert 'name="whatsapp_parameter_fields"' in form
        assert "data-whatsapp-template-feedback" in form

    assert re.search(r'(?s)class="d-none"[^>]*aria-hidden="true">.*?data-whatsapp-template-picker', create_form)
    assert re.search(r'(?s)class="d-none"[^>]*aria-hidden="true">.*?data-whatsapp-template-picker', edit_form)
    assert "Plantilla aprobada en tu cuenta de WhatsApp" not in create_form
    assert "Plantillas de Meta" not in edit_form
    assert "Configuración automática" in edit_form


def test_superadmin_campaign_form_only_requires_audience_and_message_for_normal_use():
    create_form = _read("templates/saas/crm_campaigns.html")
    edit_form = _read("templates/saas/crm_campaign_detail.html")

    assert "¿A quién queremos llegar?" in create_form
    assert "¿Qué mensaje vamos a enviar?" in create_form
    assert 'type="hidden" name="name"' in create_form
    assert 'name="subject"' in create_form
    assert 'name="body_text"' in create_form
    assert "Más filtros (opcional)" in create_form
    assert "Programar el envío para más tarde (opcional)" in create_form
    assert "Diseño avanzado de Email (opcional)" in create_form

    assert "¿A quién queremos llegar?" in edit_form
    assert "¿Qué mensaje vamos a enviar?" in edit_form
    assert 'type="hidden" name="name"' in edit_form
    assert "Más filtros (opcional)" in edit_form
    assert "Programar para más tarde (opcional)" in edit_form


def test_superadmin_review_collapses_technical_checks_and_uses_simple_actions():
    detail = _read("templates/saas/crm_campaign_detail.html")

    assert "Revisión automática" in detail
    assert "Ver detalles de la revisión" in detail
    assert "Canales de envío" in detail
    assert "Seleccionar destinatarios" in detail
    assert "Confirmar y enviar" in detail
    assert "Plantilla WhatsApp:</span>" not in detail
    assert "Motor comercial" not in detail
    assert "Preflight de envío" not in detail


def test_hidden_campaign_metadata_is_generated_without_exposing_meta_configuration():
    picker = _read("static/assets/js/superadmin-campaign-template-picker.js")
    route = _read("saas.py")

    assert "form.dataset.defaultFields" in picker
    assert "campaign_compatible" in picker
    assert "data-whatsapp-variable" in picker
    assert "StockARmobile completa automáticamente la configuración del mensaje." in picker
    assert "function startFormDefaults(form)" in picker
    assert "nameInput.value" in picker
    assert "or subject[:180]" in route
    assert "def crm_campaigns():" in route


def test_shared_commercial_navigation_avoids_crm_jargon():
    nav = _read("templates/crm/_nav.html")

    assert "Agenda comercial" in nav
    assert "Navegación comercial" in nav
    assert ">Oportunidades</a>" in nav
    assert ">Agregar contacto</a>" in nav
    assert "<i class=\"bi bi-briefcase\"></i> CRM</span>" not in nav
