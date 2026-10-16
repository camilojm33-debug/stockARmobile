from pathlib import Path
from types import SimpleNamespace

import pytest

from whatsapp_agent import _ai_order_channel_meta, _ai_order_conversation_id


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ai_order_channel_meta_maps_webchat_without_whatsapp():
    meta = _ai_order_channel_meta("webchat")
    assert meta == {"key": "webchat", "label": "Webchat", "icon": "bi-globe2"}


def test_orders_templates_do_not_hardcode_whatsapp_as_the_order_origin():
    orders = (REPO_ROOT / "templates/ai_agent/orders.html").read_text(encoding="utf-8")
    detail = (REPO_ROOT / "templates/ai_agent/order_detail.html").read_text(encoding="utf-8")
    assert "summary.channel_label" in orders
    assert "order.channel_label" in orders
    assert "order.channel_label" in detail
    assert "Seguimiento desde {{ order.channel_label }}" in detail
    assert "Origen WhatsApp" not in orders
    assert '<div class="fw-bold fs-5">WhatsApp</div>' not in detail


@pytest.mark.parametrize(
    ("external_reference", "expected"),
    [
        (
            "flow:ai_order|company_id:1|quote_id:40|conversation_id:58|user_id:1",
            58,
        ),
        ("flow:ai_order|company_id:1|quote_id:40|conversation_id:58", 58),
        ("flow:ai_order|company_id:1|quote_id:40|conversation_id:58|", 58),
        ("flow:ai_order|company_id:1|quote_id:40|user_id:1", None),
        ("flow:ai_order|company_id:1|quote_id:40|conversation_id:58x|user_id:1", None),
    ],
)
def test_ai_order_conversation_id_parses_reference_with_trailing_fields(external_reference, expected):
    payment = SimpleNamespace(external_reference=external_reference)
    assert _ai_order_conversation_id(payment) == expected
