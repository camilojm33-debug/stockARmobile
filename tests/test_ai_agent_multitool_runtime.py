import pytest

from services.ai_agent.orchestrator_v2 import AgentRuntime, MAX_TOOL_TURNS


class SequenceProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, *, messages, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("Provider called more times than expected")
        return self.responses.pop(0)


def _tool_call(name, call_id):
    return {"content": "", "tool_call": {"id": call_id, "name": name, "arguments": {}}}


def test_run_tool_loop_supports_multiple_sequential_tool_rounds(monkeypatch):
    provider = SequenceProvider(
        [
            _tool_call("buscar_producto", "call-1"),
            _tool_call("consultar_stock", "call-2"),
            {"content": "Producto encontrado y stock consultado.", "tool_call": None},
        ]
    )
    executed = []

    def fake_execute(cls, name, **kwargs):
        executed.append(name)
        return {"success": True, "tool": name}

    monkeypatch.setattr(AgentRuntime, "_execute_tool", fake_execute)

    content, campaign_context, rounds = AgentRuntime._run_tool_loop(
        provider=provider,
        messages=[{"role": "user", "content": "Necesito datos del producto"}],
        tools=[{"type": "function", "function": {"name": "buscar_producto", "parameters": {}}}],
        kwargs={},
        company_id=1,
        context={},
    )

    assert content == "Producto encontrado y stock consultado."
    assert campaign_context is None
    assert rounds == 2
    assert executed == ["buscar_producto", "consultar_stock"]
    assert len(provider.calls) == 3
    assert any(message.get("tool_call_id") == "call-1" for message in provider.calls[1]["messages"])
    assert any(message.get("tool_call_id") == "call-2" for message in provider.calls[2]["messages"])


def test_run_tool_loop_accepts_tool_calls_list(monkeypatch):
    provider = SequenceProvider(
        [
            {
                "content": "",
                "tool_calls": [
                    {"id": "call-1", "name": "buscar_producto", "arguments": {}},
                    {"id": "call-2", "name": "consultar_stock", "arguments": {}},
                ],
            },
            {"content": "Todo listo.", "tool_call": None},
        ]
    )
    executed = []

    def fake_execute(cls, name, **kwargs):
        executed.append(name)
        return {"success": True, "tool": name}

    monkeypatch.setattr(AgentRuntime, "_execute_tool", fake_execute)

    content, _, rounds = AgentRuntime._run_tool_loop(
        provider=provider,
        messages=[{"role": "user", "content": "Consulta"}],
        tools=[],
        kwargs={},
        company_id=1,
        context={},
    )

    assert content == "Todo listo."
    assert rounds == 1
    assert executed == ["buscar_producto", "consultar_stock"]
    assert len(provider.calls) == 2


def test_run_tool_loop_caps_tool_rounds_and_requests_final_synthesis(monkeypatch):
    responses = [_tool_call("buscar_producto", f"call-{index}") for index in range(MAX_TOOL_TURNS)]
    responses.append({"content": "Síntesis final.", "tool_call": None})
    provider = SequenceProvider(responses)
    executed = []

    def fake_execute(cls, name, **kwargs):
        executed.append(name)
        return {"success": True}

    monkeypatch.setattr(AgentRuntime, "_execute_tool", fake_execute)

    content, _, rounds = AgentRuntime._run_tool_loop(
        provider=provider,
        messages=[{"role": "user", "content": "Consulta"}],
        tools=[{"type": "function", "function": {"name": "buscar_producto", "parameters": {}}}],
        kwargs={},
        company_id=1,
        context={},
    )

    assert content == "Síntesis final."
    assert rounds == MAX_TOOL_TURNS + 1
    assert executed == ["buscar_producto"] * MAX_TOOL_TURNS
    assert provider.calls[-1]["tools"] == []


def test_run_tool_loop_does_not_accept_empty_provider_response():
    provider = SequenceProvider([{"content": "", "tool_call": None}])

    with pytest.raises(RuntimeError, match="no devolvió una respuesta"):
        AgentRuntime._run_tool_loop(
            provider=provider,
            messages=[{"role": "user", "content": "Consulta"}],
            tools=[],
            kwargs={},
            company_id=1,
            context={},
        )
