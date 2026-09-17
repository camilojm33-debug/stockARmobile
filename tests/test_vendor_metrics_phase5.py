from types import SimpleNamespace

from services.ai_agent import vendor_metrics_service


class _Session:
    def flush(self):
        return None


def test_record_vendor_event_is_idempotent_for_unique_key(monkeypatch):
    monkeypatch.setattr(vendor_metrics_service.db, "session", _Session())
    conversation = SimpleNamespace(id=10, metadata_json={})

    assert vendor_metrics_service.record_vendor_event(
        conversation,
        "visit",
        unique_key="visit:visitor-a",
    ) is True
    assert vendor_metrics_service.record_vendor_event(
        conversation,
        "visit",
        unique_key="visit:visitor-a",
    ) is False

    state = conversation.metadata_json[vendor_metrics_service.METRICS_KEY]
    assert state["counts"]["visit"] == 1
    assert state["seen_events"] == ["visit:visitor-a"]


def test_record_vendor_event_tracks_products(monkeypatch):
    monkeypatch.setattr(vendor_metrics_service.db, "session", _Session())
    conversation = SimpleNamespace(id=11, metadata_json={})

    vendor_metrics_service.record_vendor_event(
        conversation,
        "product_query",
        product_ids=[3, 3, 8],
    )

    state = conversation.metadata_json[vendor_metrics_service.METRICS_KEY]
    assert state["counts"]["product_query"] == 1
    assert state["products"] == {"3": 2, "8": 1}


def test_reference_value_parses_structured_payment_reference():
    reference = "flow:ai_order|company_id:4|quote_id:91|conversation_id:22"
    assert vendor_metrics_service._reference_value(reference, "quote_id") == 91
    assert vendor_metrics_service._reference_value(reference, "conversation_id") == 22
    assert vendor_metrics_service._reference_value(reference, "missing") is None
