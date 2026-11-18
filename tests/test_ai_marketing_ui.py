from pathlib import Path


def test_tenant_marketing_campaigns_screen_has_crm_visual_hierarchy():
    template = Path("templates/ai_agents/campaigns.html").read_text(encoding="utf-8")

    assert "Marketing IA" in template
    assert "crm-hero" in template
    assert "crm-kpi" in template
    assert "crm-section-card" in template
    assert "crm-status" in template
    assert "Crear campaña guiada" in template
    assert "Envío automático tras aprobar" in template
    assert "sin depender de un cron" in template
    assert "campaign_detail" in template


def test_campaign_wizard_explains_merchant_owned_whatsapp_readiness():
    wizard = Path("templates/ai_agents/campaign_wizard.html").read_text(encoding="utf-8")
    detail = Path("templates/ai_agents/campaign_detail.html").read_text(encoding="utf-8")

    assert "WhatsApp propio conectado" in wizard
    assert "WhatsApp del comercio · Vendedor IA 24/7" in wizard
    assert "cuenta WhatsApp Business del comercio" in wizard
    assert 'name="whatsapp_template"' in wizard
    assert "MARKETING compatible" in wizard
    assert 'id="campaignEditTemplate"' in detail
    assert "La selección no cambia el predeterminado del Vendedor IA" in detail
    assert "Meta Embedded Signup" in wizard
    assert "número emisor del comercio detectado" in wizard.lower()
    assert "Esta campaña saldrá exclusivamente desde el número" in wizard
    assert "Conectá el número propio del comercio desde Vendedor IA" in wizard
    assert "WhatsApp del comercio · Vendedor IA 24/7" in detail
    assert "número compartido de StockARmobile" not in wizard.lower()



def test_campaign_approval_confirmation_names_merchant_owned_sender():
    source = Path("ai_agents.py").read_text(encoding="utf-8")
    transition = source[
        source.index('def campaign_transition('):
        source.index('@bp.get("")', source.index('def campaign_transition('))
    ]

    assert "el número de WhatsApp que este comercio conectó mediante Meta" in transition
    assert "número oficial Vendedor 24/7 de StockARmobile" not in transition


def test_tenant_campaigns_have_goal_scoped_copy_templates_variables_and_preflight():
    wizard = Path("templates/ai_agents/campaign_wizard.html").read_text(encoding="utf-8")
    detail = Path("templates/ai_agents/campaign_detail.html").read_text(encoding="utf-8")
    source = Path("ai_agents.py").read_text(encoding="utf-8")
    preflight = Path("services/ai_agent/tenant_campaign_preflight.py").read_text(encoding="utf-8")

    assert "campaign_goal_options" in wizard
    assert "campaign_copy_templates" in wizard
    assert "whatsapp_parameter_fields" in wizard
    assert "whatsapp_parameter_defaults" in wizard
    assert "campaign_preflight.checks" in detail
    assert "Cómo resolverlo" in detail
    assert "campaign_preflight(campaign" in source
    assert "SCOPE_MERCHANT_CLIENTS" in preflight
    assert "SaaSLead" not in preflight


def test_superadmin_campaigns_have_prospect_only_goals_and_missing_parameter_preflight():
    wizard = Path("templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    detail = Path("templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    preflight = Path("services/saas_campaign_preflight.py").read_text(encoding="utf-8")
    engine = Path("services/campaign_template_engine.py").read_text(encoding="utf-8")

    assert "saas_acquisition" in engine
    assert "saas_trial_conversion" in engine
    assert "campaign_copy_templates" in wizard
    assert "whatsapp_parameter_defaults" in wizard
    assert "Cómo resolverlo" in detail
    assert "recipient_data" in preflight
    assert "missing_fields" in preflight
