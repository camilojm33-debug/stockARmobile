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
