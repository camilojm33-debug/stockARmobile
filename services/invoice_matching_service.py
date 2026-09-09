"""Backend-only matching for extracted invoice values."""

from __future__ import annotations

import difflib
import re
import unicodedata


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).strip().casefold()


class InvoiceMatchingService:
    @staticmethod
    def supplier(*, suppliers, name: str | None):
        key = normalize(name)
        if not key:
            return {"status": "NUEVO_PROVEEDOR_PROPUESTO", "supplier_id": None, "name": name, "candidates": []}
        matches = [item for item in suppliers if normalize(item.name) == key]
        if len(matches) == 1:
            return {"status": "MATCH_EXACTO", "supplier_id": matches[0].id, "name": matches[0].name, "candidates": []}
        if len(matches) > 1:
            return {"status": "AMBIGUO", "supplier_id": None, "name": name, "candidates": [{"id": item.id, "name": item.name, "phone": item.phone} for item in matches]}
        return {"status": "NUEVO_PROVEEDOR_PROPUESTO", "supplier_id": None, "name": name, "candidates": []}

    @classmethod
    def products(cls, *, products, items):
        result = []
        for item in items:
            code = normalize(item.get("code"))
            barcode = normalize(item.get("barcode"))
            description = normalize(item.get("description"))
            exact_code = [product for product in products if code and normalize(product.barcode) == code]
            exact_barcode = [product for product in products if barcode and normalize(product.barcode) == barcode]
            description_matches = [product for product in products if description and normalize(product.name) == description]
            matches = exact_code or exact_barcode or description_matches
            if len(matches) == 1:
                status = "MATCH_EXACTO"
                reason = "SKU" if exact_code else "BARCODE" if exact_barcode else "DESCRIPCION"
                product = matches[0]
                result.append(cls._line(item, status, product, reason))
                continue
            if len(matches) > 1:
                result.append(cls._line(item, "AMBIGUO", None, "MULTIPLE"))
                continue
            proposals = sorted(
                ((difflib.SequenceMatcher(None, description, normalize(product.name)).ratio(), product) for product in products if description),
                key=lambda pair: pair[0], reverse=True,
            )
            if proposals and proposals[0][0] >= 0.72:
                result.append(cls._line(item, "MATCH_PROPUESTO", proposals[0][1], "APROXIMADO", score=round(proposals[0][0], 3)))
            else:
                result.append(cls._line(item, "NUEVO_PRODUCTO", None, "SIN_MATCH"))
        return result

    @staticmethod
    def _line(item, status, product, reason, score=None):
        return {
            "line_number": item.get("line_number"), "description": item.get("description"), "code": item.get("code"), "barcode": item.get("barcode"),
            "quantity": item.get("quantity"), "unit_cost": item.get("unit_cost"), "line_total": item.get("line_total"), "tax_rate": item.get("tax_rate"),
            "matching_status": status, "matching_reason": reason, "product_id": product.id if product else None,
            "product_name": product.name if product else None, "product_code": product.barcode if product else None, "proposal_score": score,
        }