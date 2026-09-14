from types import SimpleNamespace

from services.invoice_matching_service import InvoiceMatchingService


def _product(pid, name, barcode="", category=""):
    return SimpleNamespace(id=pid, name=name, barcode=barcode, category=category)


def test_matching_returns_proposal_with_confidence():
    products = [_product(1, "Camiseta Algodón Blanca", "CAM-001", "Indumentaria")]
    items = [{"line_number": 1, "description": "camisetas de algodón blanca", "code": None, "barcode": None, "quantity": 2, "unit_cost": 15}]

    result = InvoiceMatchingService.products(products=products, items=items)[0]

    assert result["matching_status"] == "MATCH_PROPUESTO"
    assert result["product_id"] == 1
    assert result["proposal_score"] >= 0.72
    assert result["confidence_level"] in {"ALTA", "MEDIA"}


def test_exact_barcode_wins_over_approximate_name():
    products = [
        _product(1, "Camiseta Algodón Blanca", "CAM-001"),
        _product(2, "Camiseta Algodón Negra", "CAM-002"),
    ]
    items = [{"line_number": 1, "description": "producto distinto", "code": None, "barcode": "CAM-002", "quantity": 1, "unit_cost": 10}]

    result = InvoiceMatchingService.products(products=products, items=items)[0]

    assert result["matching_status"] == "MATCH_EXACTO"
    assert result["product_id"] == 2
    assert result["matching_reason"] == "BARCODE"
    assert result["confidence_level"] == "ALTA"


def test_low_confidence_remains_new_product():
    products = [_product(1, "Zapatos deportivos negros", "ZAP-001")]
    items = [{"line_number": 1, "description": "Tornillos inoxidables", "code": None, "barcode": None, "quantity": 3, "unit_cost": 5}]

    result = InvoiceMatchingService.products(products=products, items=items)[0]

    assert result["matching_status"] == "NUEVO_PRODUCTO"
    assert result["product_id"] is None
    assert result["confidence_level"] == "BAJA"


def test_supplier_history_can_resolve_invoice_alias():
    products = [_product(9, "Pantalón Vaquero Azul", "PAN-009")]
    items = [{"line_number": 1, "description": "Pantalones vaqueros azules", "code": None, "barcode": None, "quantity": 1, "unit_cost": 30}]
    history = [{"description": "Pantalones vaqueros azules", "product_id": 9, "supplier_id": 4}]

    result = InvoiceMatchingService.products(products=products, items=items, supplier_id=4, supplier_history=history)[0]

    assert result["matching_status"] == "MATCH_EXACTO"
    assert result["matching_reason"] == "HISTORIAL_PROVEEDOR"
    assert result["product_id"] == 9
    assert result["supplier_learning"] is True
