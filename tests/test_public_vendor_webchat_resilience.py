from pathlib import Path

from services.ai_agent.providers.gemini import GeminiProvider


def test_public_webchat_gemini_can_disable_retries():
    provider = GeminiProvider(timeout=8, max_retries=0)
    assert provider.timeout == 8
    assert provider.max_retries == 0


def test_public_webchat_runtime_is_time_bounded():
    source = Path("services/ai_agent/orchestrator_v2.py").read_text(encoding="utf-8")
    assert "PUBLIC_WEBCHAT_MAX_TOOL_TURNS = 1" in source
    assert "PUBLIC_WEBCHAT_PROVIDER_TIMEOUT = 8.0" in source
    assert "PUBLIC_WEBCHAT_MAX_OUTPUT_TOKENS = 500" in source
    assert "PUBLIC_WEBCHAT_HISTORY_LIMIT = 8" in source
    assert "max_tool_turns=PUBLIC_WEBCHAT_MAX_TOOL_TURNS if is_public_webchat else None" in source
    assert "max_retries=0 if is_public_webchat else None" in source


def test_public_vendor_tool_failure_does_not_escape_tool_loop():
    source = Path("services/ai_agent/orchestrator_v2.py").read_text(encoding="utf-8")
    assert "except ValueError as exc:" in source
    assert '"retryable": False' in source
    assert '"retryable": True' in source
    assert "AI tool execution failed" in source


def test_public_vendor_provider_failures_return_json_status():
    source = Path("services/ai_agent/vendor_publication.py").read_text(encoding="utf-8")
    assert "except AIProviderError as exc:" in source
    assert 'jsonify({"success": False, "error": str(exc)})' in source
    assert "status_code not in {429, 503}" in source


def test_preparar_pedido_can_build_cart_from_direct_product_request(monkeypatch):
    from services.ai_agent.orchestrator_v2 import VendorOrderPreviewTool, VendorCartTool
    from services.ai_agent import orchestrator_v2

    calls = []

    def fake_get_cart(**kwargs):
        calls.append(("get_cart", kwargs))
        return {"items": [], "total": 0, "currency": "ARS", "line_count": 0}

    def fake_update_cart(**kwargs):
        calls.append(("update_cart", kwargs))
        return {"success": True, "items": [{"product_id": 42, "quantity": 4}], "total": 26400, "currency": "ARS", "line_count": 1}

    def fake_create_pending_order(**kwargs):
        calls.append(("create_pending_order", kwargs))
        return {"success": True, "quote_number": "P-000042", "total": 26400}

    monkeypatch.setattr(orchestrator_v2.VendorOrderService, "get_cart", fake_get_cart)
    monkeypatch.setattr(orchestrator_v2.VendorOrderService, "update_cart", fake_update_cart)
    monkeypatch.setattr(orchestrator_v2.VendorOrderService, "create_pending_order", fake_create_pending_order)

    tool = VendorOrderPreviewTool(company_id=1, conversation_id=99, customer_phone="3655344393")
    result = tool.execute(
        product_query="machimbre",
        quantity=4,
        customer_name="Waldo Ricollini",
        customer_phone="3655344393",
        delivery_method="envio",
        delivery_address="Nueva Orleans 2332",
        delivery_city="Resistencia",
        delivery_province="Chaco",
    )

    assert result["success"] is True
    assert [item[0] for item in calls] == ["get_cart", "update_cart", "create_pending_order"]
    assert calls[1][1]["items"] == [{"product_query": "machimbre", "quantity": 4}]
    assert calls[2][1]["customer_name"] == "Waldo Ricollini"
    assert calls[2][1]["delivery_method"] == "envio"
