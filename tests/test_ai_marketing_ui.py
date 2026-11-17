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


def test_campaign_wizard_explains_shared_stockarmobile_whatsapp_readiness():
    template = Path("templates/ai_agents/campaign_wizard.html").read_text(encoding="utf-8")
    detail = Path("templates/ai_agents/campaign_detail.html").read_text(encoding="utf-8")

    assert "Canal compartido listo" in template
    assert "WhatsApp compartido de StockARmobile (Vendedor 24/7)" in template
    assert "cuenta comercial central de StockARmobile" in template
    assert "Los clientes, consentimientos, destinatarios, conversaciones y métricas siguen aislados" in template
    assert "El comercio no necesita conectar un segundo número" in template
    assert "Marketing IA todavía no está listo" in template
    assert "{% if template %}" in template
    assert "WhatsApp compartido StockARmobile · Vendedor 24/7" in detail
    assert "el comercio no necesita conectar un segundo número" in template.lower()
    assert "número emisor compartido detectado" in template.lower()
    assert "número emisor del comercio detectado" not in template.lower()
