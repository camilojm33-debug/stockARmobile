from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_superadmin_campaigns_ui_covers_all_terminal_statuses():
    template = (ROOT / "templates/saas/crm_campaigns.html").read_text(encoding="utf-8")
    for status in ("ENVIADA_PARCIAL", "FALLIDA", "SIN_ENVIO", "CANCELADA"):
        assert status in template


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
