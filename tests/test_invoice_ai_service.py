from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.invoice_ai_service import INVOICE_EXTRACTION_PROMPT, InvoiceAIError, InvoiceAIService
from services.invoice_matching_service import InvoiceMatchingService


def valid_payload(**overrides):
    payload = {
        "document_type": "FACTURA",
        "invoice_number": "0001-00000001",
        "invoice_letter": "A",
        "issue_date": "2026-09-07",
        "currency": "ARS",
        "subtotal": 100,
        "tax": 21,
        "total": 121,
        "supplier": {"name": "Proveedor Uno", "tax_id": "20-123"},
        "items": [{"line_number": 1, "code": "SKU-1", "barcode": None, "description": "Cafe", "quantity": 2, "unit": "u", "unit_cost": 50, "tax_rate": 21, "line_total": 100}],
    }
    payload.update(overrides)
    return payload


def product(product_id, name, barcode):
    return SimpleNamespace(id=product_id, name=name, barcode=barcode)


def test_valid_invoice_is_normalized_without_calling_openai():
    normalized = InvoiceAIService.validate(valid_payload())
    assert normalized["subtotal"] == Decimal("100")
    assert normalized["items"][0]["quantity"] == Decimal("2")
    assert normalized["requires_review"] is False


def test_missing_fields_are_representable_but_empty_lines_are_rejected():
    payload = valid_payload(supplier={"name": None, "tax_id": None}, invoice_number=None)
    assert InvoiceAIService.validate(payload)["supplier"]["name"] is None
    with pytest.raises(InvoiceAIError):
        InvoiceAIService.validate(valid_payload(items=[]))


def test_negative_quantity_and_cost_are_rejected():
    with pytest.raises(InvoiceAIError):
        InvoiceAIService.validate(valid_payload(items=[{**valid_payload()["items"][0], "quantity": -1}]))
    with pytest.raises(InvoiceAIError):
        InvoiceAIService.validate(valid_payload(items=[{**valid_payload()["items"][0], "unit_cost": -1}]))


def test_invalid_currency_date_and_unreasonable_tax_are_rejected():
    with pytest.raises(InvoiceAIError):
        InvoiceAIService.validate(valid_payload(currency="XYZ"))
    with pytest.raises(InvoiceAIError):
        InvoiceAIService.validate(valid_payload(issue_date="not-a-date"))
    with pytest.raises(InvoiceAIError):
        InvoiceAIService.validate(valid_payload(items=[{**valid_payload()["items"][0], "tax_rate": 101}]))


def test_inconsistent_totals_are_marked_for_review_without_autocorrection():
    normalized = InvoiceAIService.validate(valid_payload(total=999))
    assert normalized["total"] == Decimal("999")
    assert normalized["requires_review"] is True
    assert normalized["warnings"]


def test_matching_prioritizes_exact_code_and_barcode_fields():
    products = [product(1, "Cafe", "SKU-1"), product(2, "Cafe alternativo", "BAR-2")]
    lines = [{"line_number": 1, "code": "SKU-1", "barcode": None, "description": "Otra descripcion", "quantity": Decimal("1"), "unit_cost": Decimal("1"), "line_total": Decimal("1"), "tax_rate": None}]
    result = InvoiceMatchingService.products(products=products, items=lines)[0]
    assert result["matching_status"] == "MATCH_EXACTO"
    assert result["matching_reason"] == "SKU"
    assert result["product_id"] == 1


def test_matching_by_description_is_exact_and_approximation_is_only_a_proposal():
    products = [product(1, "Cafe 500g", "SKU-1")]
    exact = {"line_number": 1, "code": None, "barcode": None, "description": "Cafe 500g", "quantity": 1, "unit_cost": 1, "line_total": 1, "tax_rate": None}
    approximate = {**exact, "description": "Cafe 500 gramos"}
    assert InvoiceMatchingService.products(products=products, items=[exact])[0]["matching_status"] == "MATCH_EXACTO"
    assert InvoiceMatchingService.products(products=products, items=[approximate])[0]["matching_status"] == "MATCH_PROPUESTO"


def test_ambiguous_and_new_product_states_are_explicit():
    duplicate_products = [product(1, "Cafe", "A"), product(2, "Cafe", "B")]
    ambiguous = {"line_number": 1, "code": None, "barcode": None, "description": "Cafe", "quantity": 1, "unit_cost": 1, "line_total": 1, "tax_rate": None}
    new = {**ambiguous, "description": "Producto totalmente nuevo"}
    assert InvoiceMatchingService.products(products=duplicate_products, items=[ambiguous])[0]["matching_status"] == "AMBIGUO"
    assert InvoiceMatchingService.products(products=duplicate_products, items=[new])[0]["matching_status"] == "NUEVO_PRODUCTO"


def test_supplier_matching_never_uses_tax_id_as_a_new_model_field():
    suppliers = [SimpleNamespace(id=1, name="Proveedor Uno"), SimpleNamespace(id=2, name="Proveedor Uno")]
    assert InvoiceMatchingService.supplier(suppliers=suppliers, name="Proveedor Uno")["status"] == "AMBIGUO"
    assert InvoiceMatchingService.supplier(suppliers=[], name="Nuevo")["status"] == "NUEVO_PROVEEDOR_PROPUESTO"


def test_prompt_injection_in_document_is_data():
    assert "DATA" in INVOICE_EXTRACTION_PROMPT
    assert "prompt injection" in INVOICE_EXTRACTION_PROMPT
