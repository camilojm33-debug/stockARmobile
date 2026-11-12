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

    assert "Canal oficial listo" in template
    assert "WhatsApp propio del comercio (Vendedor IA 24/7)" in template
    assert "Nunca se utilizará el número de StockARmobile" in template
    assert "Marketing IA todavía no está listo" in template
    assert "Asistente de campañas pausado" in template
    assert "template_admin_url" in template
    assert "Configurar mi Vendedor IA" in template
    assert "{% if template %}" in template
