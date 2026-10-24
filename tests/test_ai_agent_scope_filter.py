"""Tests for deterministic pre-generation scope filtering."""
import pytest

from services.ai_agent.scope_guard import detect_out_of_scope_intent


@pytest.mark.parametrize(
    "agent_key",
    ["vendedor", "asistente", "analista", "marketing", "comercial"],
)
def test_religious_general_knowledge_request_is_blocked_for_every_agent(agent_key):
    assert detect_out_of_scope_intent(
        agent_key, "¿Qué dice la Biblia sobre el perdón?"
    ) == "religion"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("¿Quién ganó el partido de anoche?", "sports"),
        ("¿Por quién debería votar?", "politics"),
        ("Haceme la tarea sobre la fotosíntesis", "schoolwork"),
        ("Resolvé la tarea sobre la fotosíntesis", "schoolwork"),
        ("Contame algo bíblico", "religion"),
    ],
)
def test_other_high_confidence_general_topics_are_identified(message, expected):
    assert detect_out_of_scope_intent("comercial", message) == expected


@pytest.mark.parametrize(
    ("agent_key", "message"),
    [
        ("vendedor", "¿Tenés Biblias en stock?"),
        ("vendedor", "¿Cuánto sale una Biblia de estudio?"),
        ("asistente", "Mostrame el stock de productos de la empresa"),
        ("analista", "Analizá las ventas y la rentabilidad de este mes"),
        ("marketing", "Prepará una campaña para vender Biblias"),
        ("comercial", "¿Cuánto cuesta el plan de StockArMobile?"),
        ("comercial", "Quiero contratar una prueba gratuita"),
    ],
)
def test_relevant_business_requests_are_not_blocked(agent_key, message):
    assert detect_out_of_scope_intent(agent_key, message) is None


@pytest.mark.parametrize(
    ("agent_key", "message"),
    [
        ("vendedor", "Hola, ¿cómo estás?"),
        ("asistente", "Gracias por la ayuda"),
        ("analista", "¿Podés ayudarme?"),
        ("marketing", "Quiero preparar algo para mis clientes"),
        ("comercial", "Hola"),
        ("comercial", "¿Puedo hacerte una consulta?"),
    ],
)
def test_greetings_and_ambiguous_messages_are_not_blocked(agent_key, message):
    assert detect_out_of_scope_intent(agent_key, message) is None


def test_detector_is_accent_and_case_insensitive():
    assert detect_out_of_scope_intent(
        "comercial", "¿QUÉ DICE LA BÍBLIA SOBRE EL PERDÓN?"
    ) == "religion"


def test_unknown_agent_key_does_not_apply_a_wrong_scope_rule():
    assert detect_out_of_scope_intent("new-agent", "¿Qué dice la Biblia?") is None
