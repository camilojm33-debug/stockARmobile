from services.ai_agent.providers.base import AIProviderError


def test_provider_quota_error_has_http_status():
    error = AIProviderError('El servicio de IA alcanzó el límite de uso disponible para este proyecto. Intentá nuevamente más tarde.', status_code=429)
    assert error.status_code == 429
    assert 'traceback' not in str(error).lower()
