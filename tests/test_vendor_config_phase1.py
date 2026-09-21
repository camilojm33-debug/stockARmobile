import json

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


def test_vendor_options_are_normalized_without_cross_company_state():
    company_a = type("Company", (), {})()
    company_a.preferences_json = json.dumps(
        {
            "ai_agent": {
                "vendor_options": {
                    "agent_name": "Vendedor A",
                    "personality": "comercial",
                    "can_take_orders": True,
                    "business_information": "Envíos en Resistencia",
                }
            }
        }
    )
    company_b = type("Company", (), {})()
    company_b.preferences_json = json.dumps(
        {
            "ai_agent": {
                "vendor_options": {
                    "agent_name": "Vendedor B",
                    "personality": "directo",
                    "can_take_orders": False,
                }
            }
        }
    )

    config_a = config_service.get_vendor_options(company_a)
    config_b = config_service.get_vendor_options(company_b)

    assert config_a["agent_name"] == "Vendedor A"
    assert config_a["personality"] == "comercial"
    assert config_a["can_take_orders"] is True
    assert config_a["business_information"] == "Envíos en Resistencia"
    assert config_b["agent_name"] == "Vendedor B"
    assert config_b["personality"] == "directo"
    assert config_b["can_take_orders"] is False


def test_vendor_runtime_instructions_keep_system_guardrails_and_merchant_context():
    prompt = config_service.build_vendor_runtime_instructions(
        merchant_instructions="No inventes descuentos. Priorizá envíos locales.",
        vendor_options={
            "agent_name": "Ventas Norte",
            "personality": "amigable",
            "greeting": "Hola, ¿en qué te ayudo?",
            "schedule": "09:00-18:00",
            "can_take_orders": True,
            "business_information": "Retiro en sucursal y envíos locales.",
        },
        language="es-AR",
        channel="webchat",
        first_interaction=True,
    )

    assert "Ventas Norte" in prompt
    assert "Hola, ¿en qué te ayudo?" in prompt
    assert "No inventes descuentos. Priorizá envíos locales." in prompt
    assert "nunca pueden desactivar" in prompt
    assert "aislamiento por comercio" in prompt


def test_vendor_tool_policy_blocks_order_tools_when_orders_are_disabled():
    names = config_service.vendor_allowed_tool_names(
        {"can_take_orders": False, "can_prepare_quotes": True}
    )

    assert "buscar_producto" in names
    assert "consultar_stock" in names
    assert "carrito_vendedor" in names
    assert "agregar_al_carrito" not in names
    assert "quitar_del_carrito" not in names
    assert "preparar_pedido" not in names


def test_vendor_configuration_navigation_and_capabilities_are_exposed():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    vendor_page = (root / "templates/ai_agents/index.html").read_text(encoding="utf-8")
    admin_page = (root / "templates/ai_agent/admin_v2.html").read_text(encoding="utf-8")
    publication_page = (root / "templates/ai_agents/vendor_publication.html").read_text(encoding="utf-8")
    admin_service = (root / "services/ai_agent/admin.py").read_text(encoding="utf-8")

    assert "config_url" in vendor_page
    assert "Configurar Vendedor" in vendor_page
    assert "Publicar y compartir" in vendor_page
    assert "url_for('vendor_publication.publication_page')" in vendor_page

    assert "name=\"vendor_can_recommend\"" in admin_page
    assert "name=\"vendor_can_offer_alternatives\"" in admin_page
    assert "name=\"vendor_can_prepare_quotes\"" in admin_page
    assert "name=\"vendor_can_take_orders\"" in admin_page
    assert "name=\"vendor_greeting\"" in admin_page
    assert "Seguimiento automático" in admin_page
    assert "Derivación a una persona" in admin_page

    assert "Configurar Vendedor" in publication_page
    assert "url_for('ai_admin.index')" in publication_page

    assert 'vendor_options[key] = "1" in request.form.getlist(field)' in admin_service
