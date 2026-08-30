"""Read-only business metrics for the enterprise assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func

from app import Product, Sale
from services.ai_agent.tools.base import AgentTool


class ResumenVentasTool(AgentTool):
    name = "resumen_ventas"
    description = "Resume ventas reales de la empresa por cantidad, total y ticket promedio para un período en días."
    input_schema = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}},
        "required": [],
    }

    def execute(self, *, days=1, **kwargs: Any) -> Dict[str, Any]:
        try:
            days = max(1, min(int(days or 1), 365))
        except (TypeError, ValueError):
            return {"success": False, "error": "days debe ser un entero"}
        since = datetime.utcnow() - timedelta(days=days)
        query = Sale.query.filter(Sale.company_id == self.company_id, Sale.date >= since)
        count, total = query.with_entities(func.count(Sale.id), func.coalesce(func.sum(Sale.total_amount), 0)).first()
        count = int(count or 0)
        total = float(total or 0)
        return {"success": True, "days": days, "sales_count": count, "sales_total": total, "average_ticket": total / count if count else 0.0}


class StockCriticoTool(AgentTool):
    name = "stock_critico"
    description = "Lista productos activos cuyo stock está por debajo o igual al stock mínimo."
    input_schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "required": [],
    }

    def execute(self, *, limit=20, **kwargs: Any) -> Dict[str, Any]:
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit debe ser un entero"}
        products = (
            Product.query.filter(
                Product.company_id == self.company_id,
                Product.active.is_(True),
                Product.stock <= Product.min_stock,
            )
            .order_by(Product.stock.asc(), Product.name.asc())
            .limit(limit)
            .all()
        )
        return {
            "success": True,
            "items": [
                {"id": p.id, "name": p.name, "stock": float(p.stock or 0), "min_stock": float(p.min_stock or 0), "price": float(p.price or 0)}
                for p in products
            ],
        }
