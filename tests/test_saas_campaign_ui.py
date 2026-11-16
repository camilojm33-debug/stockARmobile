from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_superadmin_campaigns_ui_covers_all_terminal_statuses():
    template = (ROOT / "templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    for status in ("ENVIADA_PARCIAL", "FALLIDA", "SIN_ENVIO", "CANCELADA"):
        assert status in template


def test_campaign_ui_discloses_the_commercial_whatsapp_sender_status():
    listing = (ROOT / "templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    detail = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    crm = (ROOT / "templates/saas/crm.html").read_text(encoding="utf-8")

    assert "commercial_whatsapp.connected" in listing
    assert "commercial_whatsapp.display_phone_number" in listing
    assert "WhatsApp emisor" in detail
    assert "WhatsApp comercial" in crm


def test_superadmin_campaigns_ui_has_operational_hierarchy():
    template = (ROOT / "templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    assert "campaigns-hero" in template
    assert "campaign-kpi" in template
    assert "campaign-progress" in template
    assert "Nueva campaña" in template


def test_superadmin_campaigns_route_counts_incidents():
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    assert 'summary["INCIDENCIAS"] = summary["ENVIADA_PARCIAL"] + summary["FALLIDA"]' in source
    assert 'summary["FINALIZADAS"]' in source


def test_campaign_ui_explains_empty_audience_reasons():
    template = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    assert "Email disponible" in template
    assert "Email con permiso" in template
    assert "La audiencia filtrada está en 0." in template
    assert "Un dato importado no equivale a consentimiento de marketing." in template


def test_campaign_filters_treat_all_as_no_filter():
    source = (ROOT / "services/saas_commercial_service.py").read_text(encoding="utf-8")
    assert '"todos", "todas", "all", "*"' in source
    assert "clean_filter" in source


def test_campaign_detail_exposes_full_email_audience_action():
    template = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    assert "crm_campaign_audience_all_email" in template
    assert '/crm/campaigns/<int:campaign_id>/audience-all-email' in source


def test_campaign_detail_exposes_edit_for_drafts():
    template = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    assert "Editar campaña" in template
    assert "Guardar cambios" in template
    assert 'def crm_campaign_edit(campaign_id):' in source
    assert 'campaign.status != "BORRADOR"' in source
    assert 'saas_campaign_edit' in source


def test_campaign_edit_invalidates_previous_recipient_audience():
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    assert 'SaaSCampaignRecipient.campaign_id == campaign.id' in source
    assert 'campaign.target_count = 0' in source
    assert 'Ahora prepará nuevamente la audiencia.' in source


def test_campaign_edit_modal_keeps_save_footer_visible():
    template = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    css = (ROOT / "static/assets/css/superadmin.css").read_text(encoding="utf-8")
    assert 'id="editCampaignModal"' in template
    assert 'name="body_html"' in template
    assert 'type="submit"' in template
    assert ">Guardar cambios</button>" in template
    assert "#editCampaignModal .modal-content" in css
    assert "#editCampaignModal form" in css
    assert "#editCampaignModal .modal-body" in css
    assert "#editCampaignModal .modal-footer" in css
    assert "min-height:0" in css
    assert "position:sticky" in css


def test_campaign_creation_modal_and_edit_modal_keep_footer_outside_scroll():
    create_template = (ROOT / "templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    edit_template = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    css = (ROOT / "static/assets/css/superadmin.css").read_text(encoding="utf-8")

    assert 'id="newCampaignModal"' in create_template
    assert '>Crear borrador y continuar</button>' in create_template
    assert 'id="editCampaignModal"' in edit_template
    assert '>Guardar cambios</button>' in edit_template
    for modal_id in ("newCampaignModal", "editCampaignModal"):
        assert f"#{modal_id} .modal-content" in css
        assert f"#{modal_id} .modal-body" in css
        assert f"#{modal_id} .modal-footer" in css
    assert "overflow-y:auto !important" in css
    assert "overflow:hidden !important" in css
    assert "max-height:none !important" in css
    assert "height:calc(100dvh - 24px)" in css
    assert "modal-dialog-scrollable" not in create_template
    assert "modal-dialog-scrollable" not in edit_template
    assert f"#{'newCampaignModal'} .modal-body" in create_template
    assert f"#{'editCampaignModal'} .modal-body" in edit_template


def test_draft_campaign_can_be_deleted_from_superadmin():
    template = (ROOT / "templates/saas/crm_campaign_detail.html").read_text(encoding="utf-8")
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    assert 'url_for(' in template
    assert 'Eliminar campaña' in template
    assert 'id="deleteCampaignModal"' in template
    assert 'Confirmar eliminación' in template
    assert 'def crm_campaign_delete(campaign_id):' in source
    assert 'campaign.status not in {"BORRADOR", "CANCELADA"}' in source
    assert 'SaaSCampaignEvent.query.filter_by(campaign_id=campaign.id).delete' in source
    assert 'SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).delete' in source
    assert 'No se realizaron cambios.' in source



def test_superadmin_send_now_and_retry_target_selected_campaign():
    source = open("saas.py", encoding="utf-8").read()
    assert source.count("dispatch_due_campaigns(db.session, limit=1, per_campaign=50, campaign_id=campaign.id)") == 2


def test_superadmin_gmail_reply_flow_is_exposed_from_the_inbox():
    template = open("templates/saas/crm_inbox.html", encoding="utf-8").read()
    source = open("saas.py", encoding="utf-8").read()
    assert "url_for('saas.crm_email_reply')" in template
    assert "def crm_email_reply()" in source
    assert "send_commercial_reply(" in source
    assert "GMAIL_COMMERCIAL_SEND_SCOPE_CONFIRMED" in template



def test_gmail_watch_skipped_state_is_not_reported_as_active():
    source = open("saas.py", encoding="utf-8").read()
    assert 'if result.get("status") != "renewed":' in source
    assert "La escucha de Gmail sigue pendiente" in source


def test_gmail_reply_route_fails_closed_until_send_scope_is_confirmed():
    source = open("saas.py", encoding="utf-8").read()
    assert 'if not commercial_gmail_configuration_status().get("send_configured"):' in source
    assert "GMAIL_COMMERCIAL_SEND_SCOPE_CONFIRMED=1" in source


def test_superadmin_inbox_has_conversation_first_layout_and_mobile_navigation():
    template = (ROOT / "templates/saas/crm_inbox.html").read_text(encoding="utf-8")
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    service = (ROOT / "services/saas_commercial_service.py").read_text(encoding="utf-8")

    assert "crm-list-heading" in template
    assert "crm-message-scroll" in template
    assert "crm-composer" in template
    assert "crm-mobile-back" in template
    assert "has-selection" in template
    assert 'aria-label="Lista de conversaciones"' in template
    assert "Se enviará en el mismo hilo de Gmail." in template
    assert "latest_text = latest_text.replace(" in source
    assert 'content = f"Asunto: {str(subject or ' in service
