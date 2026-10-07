from pathlib import Path


def test_public_webchat_has_single_inflight_send_guard():
    html = Path("templates/ai_agents/public_vendor_chat.html").read_text(encoding="utf-8")
    assert "let sendInFlight = false;" in html
    assert "sendInFlight || send.disabled" in html
    assert "pendingIdempotencyKey" in html
    assert "Idempotency-Key': pendingIdempotencyKey" in html


def test_public_webchat_checks_idempotency_before_rate_limit():
    source = Path("ai_agents.py").read_text(encoding="utf-8")
    duplicate_marker = "requested_idempotency_key"
    rate_marker = "if not _public_vendor_rate_limit(company_id):"
    assert duplicate_marker in source
    assert rate_marker in source
    assert source.index('if requested_idempotency_key:') < source.index(rate_marker)
    assert "duplicate" in source
    assert "assistant_duplicate" in source
