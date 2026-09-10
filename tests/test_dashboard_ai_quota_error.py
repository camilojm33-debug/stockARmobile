from services.ai_agent.providers.base import AIProviderError


def test_ai_provider_error_message_is_safe_for_chat_response():
    error = AIProviderError(
        "El servicio de IA alcanzó el límite de uso disponible para este proyecto. Intentá nuevamente más tarde.",
        status_code=429,
    )
    assert error.status_code == 429
    assert "traceback" not in str(error).lower()
    assert "cuota" not in str(error).lower()
