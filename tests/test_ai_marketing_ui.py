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


def test_campaign_wizard_explains_merchant_whatsapp_readiness_before_showing_steps():
    template = Path("templates/ai_agents/campaign_wizard.html").read_text(encoding="utf-8")

    assert "Canal compartido listo" in template
    assert "WhatsApp compartido de StockARmobile" in template
    assert "La audiencia, los consentimientos, las conversaciones y las métricas permanecen aislados" in template
    assert "Marketing IA todavía no está listo" in template
    assert "Asistente de campañas pausado" in template
    assert "template_admin_url" in template
    assert "número compartido de Vendedor 24/7 de StockARmobile" in template
    assert "{% if template %}" in template


def test_tenant_campaigns_and_vendor_onboarding_disclose_shared_sender_without_merging_crm_data():
    campaigns = Path("templates/ai_agents/campaigns.html").read_text(encoding="utf-8")
    onboarding = Path("templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    assert "número compartido de StockARmobile" in campaigns
    assert "separados por comercio y sucursal" in campaigns
    assert "no desde este número propio del Vendedor IA" in onboarding
    assert "campañas, destinatarios, consentimientos, respuestas, bajas y métricas permanecen aislados" in onboarding
