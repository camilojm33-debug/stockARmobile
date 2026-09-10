import pytest

from services.ai_agent.providers.base import AIProviderError
from services.ai_agent.providers.gemini import GeminiProvider


class FakeQuotaError(Exception):
    code = 429


def test_daily_free_tier_quota_is_user_safe_and_not_transient(monkeypatch):
    provider = GeminiProvider(model="gemini-3.6-flash", api_key="test-key")
    provider._client = object()
    provider._types = object()
    monkeypatch.setattr(provider, "_config", lambda **kwargs: None)
    monkeypatch.setattr(provider, "_contents", lambda messages: [])

    error = FakeQuotaError(
        "429 RESOURCE_EXHAUSTED: You exceeded your current quota. "
        "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    )
    monkeypatch.setattr(provider, "_generate_content_with_retry", lambda **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(AIProviderError) as exc_info:
        provider.generate(messages=[{"role": "user", "content": "hola"}])

    assert exc_info.value.status_code == 429
    assert "límite de uso disponible" in str(exc_info.value)
    assert "865" not in str(exc_info.value)


def test_quota_retry_delay_parses_milliseconds():
    error = FakeQuotaError("Please retry in 865.016009ms.")
    assert GeminiProvider._quota_retry_delay(error) == pytest.approx(0.865016009)


def test_transient_quota_message_can_include_retry_seconds():
    error = FakeQuotaError("Please retry in 2s")
    assert GeminiProvider._format_retry_delay(GeminiProvider._quota_retry_delay(error)) == "2 segundos"
