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

    assert "WhatsApp de tu comercio listo para campañas" in wizard
    assert "Conectá WhatsApp desde Vendedor IA" in wizard
    assert "No se enviará ningún mensaje mientras tanto" in wizard
    assert "Meta Embedded Signup" not in wizard
    assert "número emisor del comercio detectado" not in wizard.lower()
    assert "El mensaje saldrá desde el WhatsApp de tu comercio." in wizard
    assert "Conectá WhatsApp desde Vendedor IA" in wizard
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
