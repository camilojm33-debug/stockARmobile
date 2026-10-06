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
