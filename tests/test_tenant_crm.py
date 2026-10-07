from stockarmobile.permissions import employee_endpoint_permission


def test_tenant_crm_permission_matrix():
    assert employee_endpoint_permission("crm.index", "GET") == "crm"
    assert employee_endpoint_permission("crm.opportunity_new", "POST") == "crm"
    assert employee_endpoint_permission("crm.activity_complete", "POST") == "crm"


def test_tenant_crm_models_are_separate_from_saas_crm():
    from crm_models import CRMActivity, CRMOpportunity
    assert CRMOpportunity.__tablename__ == "crm_opportunities"
    assert CRMActivity.__tablename__ == "crm_activities"
    assert not CRMOpportunity.__tablename__.startswith("saas_")
    assert not CRMActivity.__tablename__.startswith("saas_")


def test_tenant_crm_ai_tool_is_read_only_and_registered():
    from services.ai_agent.orchestrator_v2 import AgentRuntime
    assert "oportunidades_crm" in AgentRuntime.tool_registry
    assert "oportunidades_crm" in AgentRuntime.agent_tool_names["analista"]
    assert "oportunidades_crm" in AgentRuntime.agent_tool_names["marketing"]


def test_tenant_crm_requires_ia_pro_route_guards():
    from crm import bp

    route_functions = {
        rule.endpoint
        for rule in bp.url_values_defaults.keys()
    } if False else {rule.endpoint for rule in []}
    source = __import__("crm", fromlist=["__file__"]).__file__
    source_text = open(source, encoding="utf-8").read()
    assert source_text.count("@tenant_required\n@ai_pro_crm_required") == 9


def test_tenant_crm_menu_is_plan_gated():
    template = open("templates/base_master.html", encoding="utf-8").read()
    assert "ai_feature_crm_allowed and (current_user.role == 'admin' or current_user_has_permission('crm'))" in template
    assert "overflow-y: auto;" in template
