"""Backend-only matching for extracted invoice values.

The matcher favors deterministic identifiers first, then normalized/fuzzy
matching. High-confidence matches are accepted automatically; uncertain ones
stay as proposals so the user only reviews exceptions.
"""

from __future__ import annotations

import difflib
import re
import unicodedata


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return " ".join(text.casefold().split())


def _tokens(value: object) -> set[str]:
    return {token for token in normalize(value).split() if len(token) > 1}


def _similarity(left: object, right: object) -> float:
    a = normalize(left)
    b = normalize(right)
    if not a or not b:
        return 0.0
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    left_tokens = _tokens(a)
    right_tokens = _tokens(b)
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    containment = 1.0 if a in b or b in a else 0.0
    return max(sequence, (sequence * 0.65) + (overlap * 0.35), containment * 0.92)


class InvoiceMatchingService:
    AUTO_THRESHOLD = 0.90
    PROPOSAL_THRESHOLD = 0.72

    @staticmethod
    def supplier(*, suppliers, name: str | None):
        key = normalize(name)
        if not key:
            return {"status": "NUEVO_PROVEEDOR_PROPUESTO", "supplier_id": None, "name": name, "candidates": []}
        matches = [item for item in suppliers if normalize(item.name) == key]
        if len(matches) == 1:
            return {"status": "MATCH_EXACTO", "supplier_id": matches[0].id, "name": matches[0].name, "candidates": []}
        if len(matches) > 1:
            return {"status": "AMBIGUO", "supplier_id": None, "name": name, "candidates": [{"id": item.id, "name": item.name, "phone": getattr(item, "phone", None)} for item in matches]}
        fuzzy = sorted(
            ((_similarity(name, item.name), item) for item in suppliers),
            key=lambda pair: pair[0],
            reverse=True,
        )
        candidates = [
            {"id": item.id, "name": item.name, "phone": getattr(item, "phone", None), "score": round(score, 3)}
            for score, item in fuzzy[:3]
            if score >= 0.72
        ]
        return {"status": "NUEVO_PROVEEDOR_PROPUESTO", "supplier_id": None, "name": name, "candidates": candidates}

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
                reason = "SKU" if exact_code else "BARCODE" if exact_barcode else "DESCRIPCION"
                result.append(cls._line(item, "MATCH_EXACTO", matches[0], reason, score=1.0, auto=True))
                continue
            if len(matches) > 1:
                result.append(cls._line(item, "AMBIGUO", None, "MULTIPLE", candidates=matches[:5]))
                continue

            ranked = sorted(
                ((_similarity(description, product.name), product) for product in products if description),
                key=lambda pair: pair[0],
                reverse=True,
            )
            if ranked and ranked[0][0] >= cls.PROPOSAL_THRESHOLD:
                score, product = ranked[0]
                next_score = ranked[1][0] if len(ranked) > 1 else 0.0
                margin = score - next_score
                # A high-confidence fuzzy match is safe to accept automatically only
                # when it also has a useful separation from the second candidate.
                automatic = score >= cls.AUTO_THRESHOLD and margin >= 0.06
                status = "MATCH_EXACTO" if automatic else "MATCH_PROPUESTO"
                reason = "IA_AUTOMATICA" if automatic else "APROXIMADO"
                result.append(
                    cls._line(
                        item,
                        status,
                        product,
                        reason,
                        score=round(score, 3),
                        auto=automatic,
                        candidates=[p for _, p in ranked[:5]],
                    )
                )
            else:
                result.append(
                    cls._line(
                        item,
                        "NUEVO_PRODUCTO",
                        None,
                        "SIN_MATCH",
                        candidates=[p for _, p in ranked[:5]],
                    )
                )
        return result

    @staticmethod
    def _line(item, status, product, reason, score=None, auto=False, candidates=None):
        candidate_rows = []
        for candidate in candidates or []:
            candidate_rows.append(
                {
                    "id": candidate.id,
                    "name": candidate.name,
                    "code": getattr(candidate, "barcode", None),
                    "category": getattr(candidate, "category", None),
                }
            )
        confidence = "ALTA" if score is not None and score >= 0.90 else "MEDIA" if score is not None and score >= 0.72 else "BAJA"
        return {
            "line_number": item.get("line_number"),
            "description": item.get("description"),
            "code": item.get("code"),
            "barcode": item.get("barcode"),
            "quantity": item.get("quantity"),
            "unit_cost": item.get("unit_cost"),
            "line_total": item.get("line_total"),
            "tax_rate": item.get("tax_rate"),
            "matching_status": status,
            "matching_reason": reason,
            "product_id": product.id if product else None,
            "product_name": product.name if product else None,
            "product_code": product.barcode if product else None,
            "proposal_score": score,
            "confidence_level": confidence,
            "auto_matched": bool(auto),
            "candidate_products": candidate_rows,
        }
