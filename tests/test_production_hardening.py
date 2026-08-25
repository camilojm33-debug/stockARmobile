from pathlib import Path

from services.webhook_service import WebhookService


def test_webhook_generic_event_key_uses_notification_id():
    service = WebhookService()
    payload = {
        "id": 987654,
        "type": "subscription_preapproval",
        "data": {"id": "preapproval-123"},
        "action": "subscription.updated",
    }
    assert service._event_key(payload) == "subscription_preapproval:notification:987654"


def test_webhook_resource_id_is_fallback_only():
    service = WebhookService()
    payload = {"type": "subscription_preapproval", "data": {"id": "preapproval-123"}}
    assert service._event_key(payload) == "subscription_preapproval:preapproval-123"


def test_offline_queue_does_not_delete_auth_or_conflict_failures():
    source = Path("static/service-worker.js").read_text(encoding="utf-8")
    assert "status: 'needs_attention'" in source
    assert "response.ok || [401, 403, 409, 412]" not in source
    assert "const CACHE_NAME = 'stockarmobile-pwa-v8';" in source
