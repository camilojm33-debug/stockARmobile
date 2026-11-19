from pathlib import Path


def test_tenant_marketing_campaigns_screen_has_crm_visual_hierarchy():
    template = Path("templates/ai_agents/campaigns.html").read_text(encoding="utf-8")

    assert "Marketing IA" in template
    assert "crm-hero" in template
    assert "crm-kpi" in template
    assert "crm-section-card" in template
    assert "crm-status" in template
    assert "Crear campaña con Marketing IA" in template
    assert "crear_campana='1'" in template
    assert "Envío automático tras aprobar" in template
    assert "sin depender de un cron" in template
    assert "campaign_detail" in template


def test_campaign_creation_is_owned_by_marketing_agent_and_explains_the_flow():
    wizard_route = Path("ai_agents.py").read_text(encoding="utf-8")
    campaigns = Path("templates/ai_agents/campaigns.html").read_text(encoding="utf-8")
    marketing = Path("templates/ai_agents/index.html").read_text(encoding="utf-8")
    detail = Path("templates/ai_agents/campaign_detail.html").read_text(encoding="utf-8")
    start = wizard_route.index("def campaign_wizard():")
    end = wizard_route.index('@bp.post("/campanas/audiencia-preview")', start)
    route = wizard_route[start:end]

    assert 'return redirect(url_for("ai_agents.agent", agent="marketing", crear_campana="1"))' in route
    assert "Crear campaña con Marketing IA" in campaigns
    assert "Crear una campaña con IA" in marketing
    assert "redacta el mensaje" in marketing.lower()
    assert "Este texto lo redactó Marketing IA" in detail
    assert "Con WhatsApp y permiso registrado" in detail
    assert "No se enviará nada con el público en cero" in detail
    assert "Etapa 1 de 4" in detail
    assert "Etapa 2 de 4" in detail
    assert "Etapa 3 de 4" in detail
    assert "Etapa 4 de 4" in detail
    assert "Agregar todos los habilitados del filtro" in detail
    assert "campaign_contact_page_count" in wizard_route
    assert "campaign_selected_page_count" in wizard_route
    assert "campaign_selected_page_count" in detail
    assert "def campaign_edit(campaign_id):" in wizard_route


def test_campaign_approval_confirmation_names_merchant_owned_sender():
    source = Path("ai_agents.py").read_text(encoding="utf-8")
    transition = source[
        source.index('def campaign_transition('):
        source.index('@bp.get("")', source.index('def campaign_transition('))
    ]

    assert "el número de WhatsApp que este comercio conectó mediante Meta" in transition
    assert "número oficial Vendedor 24/7 de StockARmobile" not in transition
