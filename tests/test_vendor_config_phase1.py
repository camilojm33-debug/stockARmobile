from services.ai_agent import config_service


def test_choose_agent_maps_webchat_to_vendor(monkeypatch):
    monkeypatch.setattr(
        config_service,
        "ensure_default_agents",
        lambda company_id: {
            config_service.VENDOR_AGENT_NAME: "vendor",
            config_service.BUSINESS_AGENT_NAME: "business",
        },
    )

    assert config_service.choose_agent(123, channel="webchat") == "vendor"
    assert config_service.choose_agent(123, channel="WEBCHAT") == "vendor"
    assert config_service.choose_agent(123, channel="whatsapp") == "vendor"
    assert config_service.choose_agent(123, channel="dashboard") == "business"
