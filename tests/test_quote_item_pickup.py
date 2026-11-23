from pathlib import Path

from services.quote_pickup_service import _decimal


ROOT = Path(__file__).resolve().parents[1]


def test_pickup_quantity_parser_accepts_decimal_units_and_rejects_invalid_values():
    assert _decimal("2.500") == 2.5
    assert _decimal(3) == 3
    for value in ("-1", "NaN", "Infinity", "abc"):
        try:
            _decimal(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid pickup quantity accepted: {value}")


def test_standard_and_ai_quotes_share_item_level_pickup_service_and_endpoints():
    service = (ROOT / "services/quote_pickup_service.py").read_text(encoding="utf-8")
    standard = (ROOT / "quotes.py").read_text(encoding="utf-8")
    ai = (ROOT / "whatsapp_agent.py").read_text(encoding="utf-8")
    standard_template = (ROOT / "templates/presupuestos/view.html").read_text(encoding="utf-8")
    ai_template = (ROOT / "templates/ai_agent/order_detail.html").read_text(encoding="utf-8")

    assert "register_item_pickup" in service
    assert "register_all_remaining_pickup" in service
    assert '"/<int:quote_id>/retirar-item"' in standard
    assert '"/pedidos-ia/<int:quote_id>/retirar-item"' in ai
    assert "quote_item_id" in standard_template
    assert "quote_item_id" in ai_template
    assert "remaining_quantity" in standard_template
    assert "remaining_quantity" in ai_template
    assert "pickup_summary.picked_quantity" in ai_template


def test_pickup_event_migration_is_tenant_scoped_and_auditable():
    migration = (ROOT / "migrations/versions/20261123_01_quote_item_pickup.py").read_text(encoding="utf-8")
    model = (ROOT / "app.py").read_text(encoding="utf-8")
    service = (ROOT / "services/quote_pickup_service.py").read_text(encoding="utf-8")

    assert "quote_item_pickup_events" in migration
    assert 'ForeignKey("companies.id"' in model
    assert 'ForeignKey("quote_items.id"' in model
    assert "picked_up_by_user_id" in model
    assert "company_id=int(company_id)" in service
    assert "remaining = max(quoted - _decimal(picked_before), Decimal(\"0\"))" in service
    # Pickup records must not become a sale/payment/stock mutation pathway.
    assert "Sale(" not in service
    assert "Payment(" not in service
