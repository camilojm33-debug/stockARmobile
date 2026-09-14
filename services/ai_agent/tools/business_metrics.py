"""Read-only business metrics for the enterprise assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func

from app import Client, Product, Sale, SaleItem
from services.ai_agent.tools.base import AgentTool


def _safe_days(value: Any, default: int = 30, maximum: int = 365) -> int:
    try:
        return max(1, min(int(value or default), maximum))
    except (TypeError, ValueError):
        return default


class ResumenVentasTool(AgentTool):
    name = "resumen_ventas"
    description = "Resume ventas reales y agrega contexto de tendencia, unidades, comparación y categorías para detectar oportunidades sin inventar datos."
    input_schema = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}},
        "required": [],
    }

    def execute(self, *, days=1, **kwargs: Any) -> Dict[str, Any]:
        days = _safe_days(days, 1)
        since = datetime.utcnow() - timedelta(days=days)
        query = Sale.query.filter(Sale.company_id == self.company_id, Sale.date >= since)
        count, total = query.with_entities(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total_amount), 0),
        ).first()
        count = int(count or 0)
        total = float(total or 0)

        units = SaleItem.query.join(Sale).filter(
            Sale.company_id == self.company_id,
            Sale.date >= since,
            Sale.status.notin_(["cancelada", "anulada"]),
        ).with_entities(func.coalesce(func.sum(SaleItem.quantity), 0)).scalar()

        previous_start = since - timedelta(days=days)
        previous_query = Sale.query.filter(
            Sale.company_id == self.company_id,
            Sale.date >= previous_start,
            Sale.date < since,
        )
        previous_count, previous_total = previous_query.with_entities(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total_amount), 0),
        ).first()
        previous_count = int(previous_count or 0)
        previous_total = float(previous_total or 0)
        variation = None if previous_total == 0 else round(((total - previous_total) / previous_total) * 100, 2)

        categories = SaleItem.query.join(Sale).join(Product).filter(
            Sale.company_id == self.company_id,
            Sale.date >= since,
            Sale.status.notin_(["cancelada", "anulada"]),
            Product.company_id == self.company_id,
        ).with_entities(
            Product.category,
            func.coalesce(func.sum(SaleItem.quantity), 0).label("units"),
            func.coalesce(func.sum(SaleItem.quantity * SaleItem.price), 0).label("revenue"),
        ).group_by(Product.category).order_by(func.sum(SaleItem.quantity).desc()).limit(10).all()

        return {
            "success": True,
            "days": days,
            "sales_count": count,
            "sales_total": total,
            "average_ticket": total / count if count else 0.0,
            "units_sold": float(units or 0),
            "comparison": {
                "previous_sales_count": previous_count,
                "previous_sales_total": previous_total,
                "variation_percent": variation,
                "trend": "creciente" if variation is not None and variation > 2 else "decreciente" if variation is not None and variation < -2 else "estable" if variation is not None else "sin_base",
            },
            "top_categories": [
                {"category": row.category or "Sin categoría", "units": float(row.units or 0), "revenue": float(row.revenue or 0)}
                for row in categories
            ],
            "data_quality": "real_db_aggregates",
        }


class StockCriticoTool(AgentTool):
    name = "stock_critico"
    description = "Lista productos activos cuyo stock está por debajo o igual al stock mínimo e indica la brecha y el valor inmovilizado a costo."
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
                {
                    "id": p.id,
                    "name": p.name,
                    "stock": float(p.stock or 0),
                    "min_stock": float(p.min_stock or 0),
                    "stock_gap": max(0.0, float(p.min_stock or 0) - float(p.stock or 0)),
                    "price": float(p.price or 0),
                    "cost_price": float(p.cost_price or 0),
                    "stock_value_at_cost": float(p.stock or 0) * float(p.cost_price or 0),
                }
                for p in products
            ],
        }


class ProductosMasVendidosTool(AgentTool):
    name = "productos_mas_vendidos"
    description = "Lista productos más vendidos con unidades, facturación y margen estimado basados en las ventas reales del período."
    input_schema = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": [],
    }

    def execute(self, *, days=30, limit=10, **kwargs: Any) -> Dict[str, Any]:
        days = _safe_days(days, 30)
        try:
            limit = max(1, min(int(limit or 10), 20))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit debe ser un entero"}
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
            .with_entities(
                Product.id,
                Product.name,
                Product.category,
                func.sum(SaleItem.quantity).label("quantity"),
                func.coalesce(func.sum(SaleItem.quantity * SaleItem.price), 0).label("revenue"),
                func.coalesce(func.sum(SaleItem.quantity * SaleItem.cost_price), 0).label("cost"),
            )
            .group_by(Product.id, Product.name, Product.category)
            .order_by(func.sum(SaleItem.quantity).desc(), Product.name.asc())
            .limit(limit)
            .all()
        )
        items = []
        for row in rows:
            revenue = float(row.revenue or 0)
            cost = float(row.cost or 0)
            items.append({
                "id": row.id,
                "name": row.name,
                "category": row.category or "Sin categoría",
                "quantity": float(row.quantity or 0),
                "revenue": revenue,
                "cost": cost,
                "gross_margin": revenue - cost,
                "gross_margin_percent": round(((revenue - cost) / revenue) * 100, 2) if revenue else None,
            })
        return {"success": True, "days": days, "items": items}


class ContarProductosTool(AgentTool):
    name = "contar_productos"
    description = "Cuenta productos reales y agrega cuántos están activos o en stock crítico."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> Dict[str, Any]:
        total_products = int(Product.query.filter(Product.company_id == self.company_id).count())
        active_products = int(Product.query.filter(Product.company_id == self.company_id, Product.active.is_(True)).count())
        critical_products = int(Product.query.filter(
            Product.company_id == self.company_id,
            Product.active.is_(True),
            Product.stock <= Product.min_stock,
        ).count())
        return {
            "success": True,
            "total_products": total_products,
            "active_products": active_products,
            "critical_stock_products": critical_products,
        }


class ProductosSinVentasRecientesTool(AgentTool):
    name = "productos_sin_ventas_recientes"
    description = "Lista productos activos sin ventas recientes y aporta stock, valor inmovilizado y días desde la última venta."
    input_schema = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": [],
    }

    def execute(self, *, days=90, limit=20, **kwargs: Any) -> Dict[str, Any]:
        days = _safe_days(days, 90)
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit debe ser un entero"}
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

        last_sales = {}
        if products:
            ids = [product.id for product in products]
            rows = SaleItem.query.join(Sale).filter(
                Sale.company_id == self.company_id,
                Sale.status.notin_(["cancelada", "anulada"]),
                SaleItem.product_id.in_(ids),
            ).with_entities(SaleItem.product_id, func.max(Sale.date)).group_by(SaleItem.product_id).all()
            last_sales = {product_id: date for product_id, date in rows}

        now = datetime.utcnow()
        items = []
        for product in products:
            last_sale = last_sales.get(product.id)
            items.append({
                "id": product.id,
                "name": product.name,
                "category": product.category or "Sin categoría",
                "stock": float(product.stock or 0),
                "price": float(product.price or 0),
                "cost_price": float(product.cost_price or 0),
                "stock_value_at_cost": float(product.stock or 0) * float(product.cost_price or 0),
                "last_sale_at": last_sale.isoformat() if last_sale else None,
                "days_since_last_sale": (now - last_sale).days if last_sale else None,
            })
        return {"success": True, "days": days, "items": items}


class ContarClientesTool(AgentTool):
    name = "contar_clientes"
    description = "Cuenta clientes activos reales de la empresa actual."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> Dict[str, Any]:
        active = int(Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True)).count())
        total = int(Client.query.filter(Client.company_id == self.company_id).count())
        return {"success": True, "active_clients": active, "total_clients": total}
