from pathlib import Path

import pytest

from whatsapp_agent import _ai_order_row, _ai_order_channel_meta


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pickup_status_is_exposed_in_ai_order_summary_without_changing_commercial_state(monkeypatch):
    from types import SimpleNamespace
    import whatsapp_agent

    monkeypatch.setattr(whatsapp_agent, "_ai_order_payment", lambda company_id, quote_id: None)
    monkeypatch.setattr(whatsapp_agent, "_ai_order_conversation", lambda company_id, quote_id, payment=None: None)
    monkeypatch.setattr(whatsapp_agent, "_ai_order_channel", lambda company_id, quote_id, payment=None: "webchat")
    monkeypatch.setattr(whatsapp_agent, "get_vendor_options", lambda company: {"can_follow_up": False})
    monkeypatch.setattr(whatsapp_agent, "Company", SimpleNamespace(query=SimpleNamespace(filter_by=lambda **kwargs: SimpleNamespace(first=lambda: SimpleNamespace(id=1)))))
    monkeypatch.setattr(whatsapp_agent, "db", SimpleNamespace(session=SimpleNamespace()))
    # Avoid importing QuoteDelivery or hitting the database: _ai_order_row reads only attributes below.
    import app as stock_app
    monkeypatch.setattr(stock_app, "Company", SimpleNamespace(query=SimpleNamespace(filter_by=lambda **kwargs: SimpleNamespace(first=lambda: SimpleNamespace(id=1)))))

    delivery = SimpleNamespace(
        method="retiro",
        pickup_status="picked_up",
        picked_up_at=None,
        picked_up_by=SimpleNamespace(name="Operador"),
        shipping_status="confirmed",
    )
    quote = SimpleNamespace(
        id=42, number="P-000042", company_id=1, observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
        converted_sale_id=8, total_amount=Decimal("100.00"), currency="ARS", items=[], date=None, created_at=None,
        client=None, consumer_name="Cliente", client_id=None, delivery=delivery,
    )
    row = _ai_order_row(1, quote)
    assert row["order_key"] == "confirmed"
    assert row["sale_id"] == 8
    assert row["delivery"]["pickup_status"] == "picked_up"
    assert row["delivery"]["picked_up_by"] == "Operador"


def test_pickup_tracking_is_shown_in_both_ai_order_and_quote_screens():
    ai_detail = (REPO_ROOT / "templates/ai_agent/order_detail.html").read_text(encoding="utf-8")
    ai_orders = (REPO_ROOT / "templates/ai_agent/orders.html").read_text(encoding="utf-8")
    quote_detail = (REPO_ROOT / "templates/presupuestos/view.html").read_text(encoding="utf-8")
    quote_index = (REPO_ROOT / "templates/presupuestos/index.html").read_text(encoding="utf-8")

    assert "Marcar retirado" in ai_detail
    assert "delivery.picked_up_at" in ai_detail
    assert "pickup_pending" in ai_orders
    assert "picked_up" in ai_orders
    assert "Seguimiento de entrega" in quote_detail
    assert "Listo para retirar" in quote_index


def test_pickup_migration_does_not_backfill_historical_deliveries_as_picked_up():
    migration = (REPO_ROOT / "migrations/versions/20261028_02_quote_pickup_tracking.py").read_text(encoding="utf-8")
    assert "picked_up_at" in migration
    assert "UPDATE quote_deliveries" not in migration
    assert "pickup_status" in migration
