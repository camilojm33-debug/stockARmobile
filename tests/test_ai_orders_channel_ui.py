from pathlib import Path

from whatsapp_agent import _ai_order_channel_meta


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
