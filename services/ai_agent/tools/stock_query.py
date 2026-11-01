"""Stock query tool for the AI Agent."""

from __future__ import annotations

from typing import Any, Dict

from app import Branch, BranchStock, Product
from services.ai_agent.tools.base import AgentTool
from services.ai_agent.stock_quantity import normalized_stock_quantity


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

        from services.ai_agent.tools.branch_scope import employee_branch_scope
        restricted, visible_branch_id = employee_branch_scope(effective_company_id)
        stock_query = BranchStock.query.filter_by(
            company_id=int(effective_company_id), product_id=product.id
        ).join(Branch, Branch.id == BranchStock.branch_id).filter(Branch.active.is_(True))
        if restricted:
            stock_query = stock_query.filter(BranchStock.branch_id == visible_branch_id)
        branch_rows = stock_query.order_by(Branch.name.asc()).all()
        current_stock = (
            normalized_stock_quantity(branch_rows[0].stock)
            if restricted and branch_rows
            else 0.0 if restricted
            else normalized_stock_quantity(product.stock)
        )
        return {
            "success": True,
            "product": {
                "id": product.id,
                "name": product.name,
                "stock": current_stock,
                "stock_by_branch": [
                    {"branch_id": row.branch_id, "branch_name": row.branch.name,
                     "stock": normalized_stock_quantity(row.stock)}
                    for row in branch_rows
                ],
                "price": float(product.price) if getattr(product, "price", None) is not None else None,
            },
        }
