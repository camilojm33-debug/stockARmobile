from pathlib import Path


def test_ia_pro_plan_presents_crm_as_included_feature():
    html = Path("templates/ai_agents/index.html").read_text(encoding="utf-8")

    assert '<span>CRM comercial</span>' in html
    assert '<td>CRM comercial</td><td>—</td><td>—</td><td>—</td><td>✓</td>' in html
    assert "CRM comercial, Marketing IA" in html


def test_ia_pro_crm_entitlement_is_still_enforced_by_plan_code():
    source = Path("services/ai_agent/usage_service.py").read_text(encoding="utf-8")

    assert 'if key == "crm" and plan["code"] != "pro":' in source
    assert "CRM comercial requiere IA PRO." in source


def test_ai_plan_comparison_matches_backend_rollback_entitlement():
    html = Path("templates/ai_agents/index.html").read_text(encoding="utf-8")

    assert '<tr><td>Rollback de precios</td><td>—</td><td>—</td><td>—</td><td>✓</td></tr>' in html
    assert '<tr><td>Rollback de precios</td><td>—</td><td>—</td><td>✓</td><td>✓</td></tr>' not in html
    assert html.count('<tr><td>Optimización de precios con IA</td>') == 1
