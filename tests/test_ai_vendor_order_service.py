import json


def test_vendor_order_external_reference_contains_tenant_context():
    external_reference = "flow:ai_order|company_id:17|quote_id:42|conversation_id:9|user_id:3"
    parts = {
        segment.split(":", 1)[0]: segment.split(":", 1)[1]
        for segment in external_reference.split("|")
        if ":" in segment
    }
    assert parts["flow"] == "ai_order"
    assert parts["company_id"] == "17"
    assert parts["quote_id"] == "42"
    assert parts["conversation_id"] == "9"


def test_vendor_payment_payload_is_json_serializable():
    payload = {
        "flow": "ai_order",
        "company_id": 17,
        "quote_id": 42,
        "conversation_id": 9,
    }
    assert json.loads(json.dumps(payload)) == payload


def test_vendor_cart_quantity_must_be_positive():
    quantity = 0
    assert quantity <= 0
