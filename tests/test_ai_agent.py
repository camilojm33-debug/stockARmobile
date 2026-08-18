import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest

import app as stock_app
import services.ai_agent.orchestrator as ai_orchestrator_module
from app import Client, Company, Product, db
from services.ai_agent.orchestrator import AgentOrchestrator
from services.ai_agent.tools.customer_search import BuscarClienteTool
from services.ai_agent.tools.product_search import BuscarProductoTool
from services.ai_agent.tools.stock_query import ConsultarStockTool
from stockarmobile.models.conversations import (
    Agent,
    AgentConfiguration,
    Conversation,
    ConversationMessage,
)


@pytest.fixture
def ai_database():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()

        company_a = Company(name="Empresa A", active=True)
        company_b = Company(name="Empresa B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()

        product_a = Product(
            barcode="111111",
            name="Producto A",
            price=100,
            cost_price=50,
            stock=10,
            min_stock=1,
            active=True,
            company_id=company_a.id,
        )

        product_b = Product(
            barcode="222222",
            name="Producto B",
            price=200,
            cost_price=100,
            stock=20,
            min_stock=1,
            active=True,
            company_id=company_b.id,
        )

        client_a = Client(
            name="Cliente A",
            email="cliente.a@test.local",
            phone="111111",
            active=True,
            company_id=company_a.id,
        )

        client_b = Client(
            name="Cliente B",
            email="cliente.b@test.local",
            phone="222222",
            active=True,
            company_id=company_b.id,
        )

        db.session.add_all([product_a, product_b, client_a, client_b])
        db.session.commit()

        yield {
            "company_a": company_a,
            "company_b": company_b,
            "product_a": product_a,
            "product_b": product_b,
            "client_a": client_a,
            "client_b": client_b,
        }

        db.session.remove()
        db.drop_all()


def test_buscar_producto_aisla_company(ai_database):
    data = ai_database

    tool = BuscarProductoTool(company_id=data["company_a"].id)

    own = tool.execute(query="Producto A")
    other = tool.execute(query="Producto B")

    assert own["success"] is True
    assert [item["id"] for item in own["items"]] == [data["product_a"].id]

    assert other["success"] is True
    assert other["items"] == []


def test_consultar_stock_aisla_company(ai_database):
    data = ai_database

    tool = ConsultarStockTool(company_id=data["company_a"].id)

    own = tool.execute(product_id=data["product_a"].id)
    other = tool.execute(product_id=data["product_b"].id)

    assert own["success"] is True
    assert own["product"]["stock"] == 10.0

    assert other["success"] is False
    assert other["error"] == "product_not_found"


def test_buscar_cliente_aisla_company(ai_database):
    data = ai_database

    tool = BuscarClienteTool(company_id=data["company_a"].id)

    own = tool.execute(query="Cliente A")
    other = tool.execute(query="Cliente B")

    assert own["success"] is True
    assert [item["id"] for item in own["items"]] == [data["client_a"].id]

    assert other["success"] is True
    assert other["items"] == []


def test_tool_rechaza_company_id_inconsistente(ai_database):
    data = ai_database

    tool = BuscarProductoTool(company_id=data["company_a"].id)

    result = tool.execute(
        query="Producto A",
        company_id=data["company_b"].id,
    )

    assert result["success"] is False
    assert result["error"] == "company_id mismatch"


def test_orchestrator_rechaza_conversation_de_otra_company(ai_database):
    data = ai_database

    conversation = Conversation(
        company_id=data["company_b"].id,
        channel="test",
        status="open",
    )
    db.session.add(conversation)
    db.session.commit()

    with pytest.raises(ValueError, match="Conversation not found"):
        AgentOrchestrator.handle_message(
            company_id=data["company_a"].id,
            conversation_id=conversation.id,
            message="Hola",
        )


def test_orchestrator_rechaza_tool_desconocida(ai_database):
    data = ai_database

    result = AgentOrchestrator.execute_tool(
        "tool_inexistente",
        company_id=data["company_a"].id,
        arguments={},
    )

    assert result["success"] is False
    assert result["error"] == "tool_not_found"


def test_orchestrator_rechaza_company_id_del_modelo(ai_database):
    data = ai_database

    result = AgentOrchestrator.execute_tool(
        "buscar_producto",
        company_id=data["company_a"].id,
        arguments={
            "query": "Producto A",
            "company_id": data["company_b"].id,
        },
    )

    assert result["success"] is False
    assert result["error"] == "company_id must be passed explicitly"


class FakeProvider:
    def __init__(self):
        self.calls = []

    def generate(self, **payload):
        self.calls.append(payload)
        return {"content": "respuesta fake"}


def test_orchestrator_idempotencia(ai_database, monkeypatch):
    data = ai_database

    conversation = Conversation(
        company_id=data["company_a"].id,
        channel="test",
        status="open",
    )
    db.session.add(conversation)
    db.session.commit()

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    first = AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="Hola",
        metadata={"idempotency_key": "test-idempotency-1"},
    )

    second = AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="Hola",
        metadata={"idempotency_key": "test-idempotency-1"},
    )

    assert first["status"] == "completed"
    assert second["status"] == "duplicate"
    assert second["message_id"] == first["message_id"]
    assert len(fake_provider.calls) == 1


def test_orchestrator_build_tool_uses_explicit_company_id(ai_database):
    data = ai_database

    tool = AgentOrchestrator.build_tool(
        "buscar_producto",
        company_id=data["company_a"].id,
        query="Producto A",
    )

    assert tool is not None
    assert tool.company_id == data["company_a"].id
    assert tool.execute(query="Producto A")["items"][0]["id"] == data["product_a"].id


def test_orchestrator_integration_flow_with_fake_provider(ai_database, monkeypatch):
    data = ai_database

    conversation = Conversation(
        company_id=data["company_a"].id,
        channel="test",
        status="open",
    )
    db.session.add(conversation)
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)

            if len(self.calls) == 1:
                tools = payload.get("tools") or []
                assert any(tool["function"]["name"] == "buscar_producto" for tool in tools)
                return {
                    "content": "",
                    "tool_call": {
                        "name": "buscar_producto",
                        "arguments": {"query": "Producto A"},
                    },
                }

            messages = payload.get("messages") or []
            assert messages[-1]["role"] == "tool"
            tool_result = json.loads(messages[-1]["content"])

            assert tool_result["success"] is True
            assert [item["id"] for item in tool_result["items"]] == [data["product_a"].id]
            assert data["product_b"].id not in [item["id"] for item in tool_result["items"]]
            return {"content": "Encontré Producto A.", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    result = AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="Hola",
    )

    assert result["status"] == "completed"
    assert result["content"] == "Encontré Producto A."
    assert result["company_id"] == data["company_a"].id
    assert result["conversation_id"] == conversation.id
    assert len(fake_provider.calls) == 2

    first_call = fake_provider.calls[0]
    second_call = fake_provider.calls[1]

    assert first_call["messages"] == [{"role": "user", "content": "Hola"}]
    assert any(tool["function"]["name"] == "buscar_producto" for tool in first_call["tools"])

    assert second_call["messages"][1]["tool_calls"][0]["function"]["name"] == "buscar_producto"
    tool_result = json.loads(second_call["messages"][-1]["content"])
    assert tool_result["success"] is True
    assert tool_result["items"][0]["id"] == data["product_a"].id
    assert data["product_b"].id not in [item["id"] for item in tool_result["items"]]

    assert result["message_id"]
    assert result["assistant_message_id"]


def test_conversation_history_is_sent_to_provider(ai_database, monkeypatch):
    data = ai_database

    conversation = Conversation(
        company_id=data["company_a"].id,
        channel="test",
        status="open",
    )
    db.session.add(conversation)
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)
            return {"content": "respuesta final", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    first = AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="Buscá Coca Cola",
    )
    second = AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="¿Cuánto sale?",
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert len(fake_provider.calls) == 2

    second_payload = fake_provider.calls[1]
    messages = second_payload["messages"]
    assert any(message["role"] == "user" and message["content"] == "Buscá Coca Cola" for message in messages)
    assert messages[-1] == {"role": "user", "content": "¿Cuánto sale?"}


def test_conversation_history_isolated_by_company(ai_database, monkeypatch):
    data = ai_database

    conversation_a = Conversation(company_id=data["company_a"].id, channel="test", status="open")
    conversation_b = Conversation(company_id=data["company_b"].id, channel="test", status="open")
    db.session.add_all([conversation_a, conversation_b])
    db.session.commit()

    db.session.add_all(
        [
            ConversationMessage(
                conversation_id=conversation_b.id,
                company_id=data["company_b"].id,
                sender_type="user",
                role="user",
                content="Mensaje de B",
                content_type="text",
            ),
            ConversationMessage(
                conversation_id=conversation_b.id,
                company_id=data["company_b"].id,
                sender_type="agent",
                role="assistant",
                content="Respuesta de B",
                content_type="text",
            ),
        ]
    )
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)
            return {"content": "ok", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation_a.id,
        message="Mensaje de A",
    )

    payload = fake_provider.calls[0]
    contents = [message["content"] for message in payload["messages"]]
    assert "Mensaje de B" not in contents
    assert "Respuesta de B" not in contents
    assert "Mensaje de A" in contents


def test_conversation_history_isolated_by_conversation(ai_database, monkeypatch):
    data = ai_database

    conversation_1 = Conversation(company_id=data["company_a"].id, channel="test", status="open")
    conversation_2 = Conversation(company_id=data["company_a"].id, channel="test", status="open")
    db.session.add_all([conversation_1, conversation_2])
    db.session.commit()

    db.session.add_all(
        [
            ConversationMessage(
                conversation_id=conversation_1.id,
                company_id=data["company_a"].id,
                sender_type="user",
                role="user",
                content="Historial de conv 1",
                content_type="text",
            ),
            ConversationMessage(
                conversation_id=conversation_1.id,
                company_id=data["company_a"].id,
                sender_type="agent",
                role="assistant",
                content="Respuesta conv 1",
                content_type="text",
            ),
        ]
    )
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)
            return {"content": "ok", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation_2.id,
        message="Solo conv 2",
    )

    payload = fake_provider.calls[0]
    contents = [message["content"] for message in payload["messages"]]
    assert "Historial de conv 1" not in contents
    assert "Respuesta conv 1" not in contents
    assert "Solo conv 2" in contents


def test_conversation_history_preserves_order(ai_database, monkeypatch):
    data = ai_database

    conversation = Conversation(company_id=data["company_a"].id, channel="test", status="open")
    db.session.add(conversation)
    db.session.commit()

    db.session.add_all(
        [
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="user",
                role="user",
                content="Primero",
                content_type="text",
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="agent",
                role="assistant",
                content="Segundo",
                content_type="text",
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="user",
                role="user",
                content="Tercero",
                content_type="text",
            ),
        ]
    )
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)
            return {"content": "ok", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="Cuarto",
    )

    messages = fake_provider.calls[0]["messages"]
    content_order = [message["content"] for message in messages]
    assert content_order == ["Primero", "Segundo", "Tercero", "Cuarto"]


def test_tool_flow_preserves_context(ai_database, monkeypatch):
    data = ai_database

    conversation = Conversation(company_id=data["company_a"].id, channel="test", status="open")
    db.session.add(conversation)
    db.session.commit()

    db.session.add_all(
        [
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="user",
                role="user",
                content="Buscá Coca Cola",
                content_type="text",
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="agent",
                role="assistant",
                content=json.dumps({"tool_call": {"name": "buscar_producto", "arguments": {"query": "Coca Cola"}}}, ensure_ascii=False),
                content_type="json",
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="tool",
                role="tool",
                content=json.dumps({"success": True, "items": [{"id": data["product_a"].id, "name": "Producto A"}]}, ensure_ascii=False),
                content_type="json",
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=data["company_a"].id,
                sender_type="agent",
                role="assistant",
                content="Encontré Producto A.",
                content_type="text",
            ),
        ]
    )
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)
            return {"content": "Precio del producto: 100", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="¿Cuánto sale?",
    )

    messages = fake_provider.calls[0]["messages"]
    content_payload = [message["content"] for message in messages]
    assert "Buscá Coca Cola" in content_payload
    assert "Encontré Producto A." in content_payload
    assert "¿Cuánto sale?" in content_payload


def test_history_has_reasonable_limit(ai_database, monkeypatch):
    data = ai_database

    conversation = Conversation(company_id=data["company_a"].id, channel="test", status="open")
    db.session.add(conversation)
    db.session.commit()

    messages = [
        ConversationMessage(
            conversation_id=conversation.id,
            company_id=data["company_a"].id,
            sender_type="user",
            role="user",
            content=f"mensaje {index}",
            content_type="text",
        )
        for index in range(30)
    ]
    db.session.add_all(messages)
    db.session.commit()

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def generate(self, **payload):
            self.calls.append(payload)
            return {"content": "ok", "tool_call": None}

    fake_provider = FakeProvider()
    monkeypatch.setattr(ai_orchestrator_module, "LMStudioProvider", lambda: fake_provider)

    AgentOrchestrator.handle_message(
        company_id=data["company_a"].id,
        conversation_id=conversation.id,
        message="mensaje final",
    )

    payload_messages = fake_provider.calls[0]["messages"]
    assert len(payload_messages) <= 20
    assert payload_messages[-1] == {"role": "user", "content": "mensaje final"}
