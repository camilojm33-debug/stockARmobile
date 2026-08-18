"""Stock query tool for the AI Agent."""

from __future__ import annotations

from typing import Any, Dict

from app import Product
from services.ai_agent.tools.base import AgentTool


class ConsultarStockTool(AgentTool):
    """Return the stock quantity for a product in a specific company scope."""

    name = "consultar_stock"
    description = "Consulta el stock actual de un producto dentro de una empresa específica."
    input_schema = {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer"},
        },
        "required": ["product_id"],
    }

    def __init__(self, *, company_id: Any, **kwargs: Any) -> None:
        super().__init__(company_id=company_id, **kwargs)

    def execute(self, *, company_id=None, product_id=None, **kwargs: Any) -> Dict[str, Any]:
        effective_company_id = self.company_id if company_id is None else company_id
        if effective_company_id in (None, ""):
            return {"success": False, "error": "company_id is required", "product": None}

        if product_id in (None, ""):
            return {"success": False, "error": "product_id is required", "product": None}

        if company_id is not None and company_id != self.company_id:
            return {"success": False, "error": "company_id mismatch", "product": None}

        if "company_id" in (kwargs or {}):
            return {
                "success": False,
                "error": "company_id must be passed explicitly and cannot be inferred from metadata",
                "product": None,
            }

        product = (
            Product.query.filter(
                Product.id == product_id,
                Product.company_id == effective_company_id,
            )
            .first()
        )

        if product is None:
            return {"success": False, "error": "product_not_found", "product": None}

        return {
            "success": True,
            "product": {
                "id": product.id,
                "name": product.name,
                "stock": float(product.stock) if product.stock is not None else 0.0,
                "price": float(product.price) if getattr(product, "price", None) is not None else None,
            },
        }
