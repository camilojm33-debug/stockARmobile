"""Read-only business metrics for the enterprise assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func

from app import Client, Product, Sale, SaleItem
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


class ProductosMasVendidosTool(AgentTool):
    name = "productos_mas_vendidos"
    description = "Lista los productos con mayor cantidad vendida en un período real de la empresa."
    input_schema = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        "required": [],
    }

    def execute(self, *, days=30, limit=10, **kwargs: Any) -> Dict[str, Any]:
        try:
            days = max(1, min(int(days or 30), 365))
            limit = max(1, min(int(limit or 10), 20))
        except (TypeError, ValueError):
            return {"success": False, "error": "days y limit deben ser enteros"}
        since = datetime.utcnow() - timedelta(days=days)
        rows = (
            SaleItem.query.join(Sale).join(Product)
            .filter(
                Sale.company_id == self.company_id,
                Sale.date >= since,
                Sale.status.notin_(["cancelada", "anulada"]),
                Product.company_id == self.company_id,
                Product.active.is_(True),
            )
            .with_entities(Product.id, Product.name, func.sum(SaleItem.quantity).label("quantity"))
            .group_by(Product.id, Product.name)
            .order_by(func.sum(SaleItem.quantity).desc(), Product.name.asc())
            .limit(limit).all()
        )
        return {"success": True, "days": days, "items": [{"id": row.id, "name": row.name, "quantity": float(row.quantity or 0)} for row in rows]}


class ProductosSinVentasRecientesTool(AgentTool):
    name = "productos_sin_ventas_recientes"
    description = "Lista productos activos de la empresa que no registran ventas en los últimos días indicados."
    input_schema = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "required": [],
    }

    def execute(self, *, days=90, limit=20, **kwargs: Any) -> Dict[str, Any]:
        try:
            days = max(1, min(int(days or 90), 365))
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "days y limit deben ser enteros"}
        since = datetime.utcnow() - timedelta(days=days)
        sold_ids = SaleItem.query.join(Sale).filter(
            Sale.company_id == self.company_id,
            Sale.date >= since,
            Sale.status.notin_(["cancelada", "anulada"]),
        ).with_entities(SaleItem.product_id).distinct()
        products = Product.query.filter(
            Product.company_id == self.company_id,
            Product.active.is_(True),
            ~Product.id.in_(sold_ids),
        ).order_by(Product.name.asc()).limit(limit).all()
        return {"success": True, "days": days, "items": [{"id": product.id, "name": product.name, "stock": float(product.stock or 0)} for product in products]}


class ContarClientesTool(AgentTool):
    name = "contar_clientes"
    description = "Cuenta clientes activos reales de la empresa actual."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> Dict[str, Any]:
        return {"success": True, "active_clients": int(Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True)).count())}
