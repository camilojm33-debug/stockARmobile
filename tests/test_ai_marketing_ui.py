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
    assert "cuenta WhatsApp Business conectada por este comercio" in wizard
    assert "Meta Embedded Signup" in wizard
    assert "número emisor del comercio detectado" in wizard.lower()
    assert "Esta campaña saldrá exclusivamente desde el número" in wizard
    assert "Conectá el número propio del comercio desde Vendedor IA" in wizard
    assert "WhatsApp del comercio · Vendedor IA 24/7" in detail
    assert "número compartido de StockARmobile" not in wizard.lower()
