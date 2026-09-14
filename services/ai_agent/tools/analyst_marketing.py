"""Tenant-scoped read-only tools for analyst and marketing conversations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func

from app import Client, Product, Sale, SaleItem
from services.ai_agent.tools.base import AgentTool


def _days(value: Any, default: int) -> int:
    try:
        return max(1, min(int(value or default), 365))
    except (TypeError, ValueError):
        return default


def _date_limit(days: Any, default: int) -> datetime:
    return datetime.utcnow() - timedelta(days=_days(days, default))


def _category_period(company_id: int, start: datetime, end: datetime) -> Dict[str, Dict[str, float]]:
    rows = SaleItem.query.join(Sale).join(Product).filter(
        Sale.company_id == company_id,
        Sale.date >= start,
        Sale.date < end,
        Sale.status.notin_(["cancelada", "anulada"]),
        Product.company_id == company_id,
    ).with_entities(
        Product.category,
        func.coalesce(func.sum(SaleItem.quantity), 0).label("units"),
        func.coalesce(func.sum(SaleItem.quantity * SaleItem.price), 0).label("revenue"),
    ).group_by(Product.category).all()
    return {
        (row.category or "Sin categoría"): {
            "units": float(row.units or 0),
            "revenue": float(row.revenue or 0),
        }
        for row in rows
    }


class VentasComparativaTool(AgentTool):
    name = "comparar_ventas"
    description = "Compara ventas reales de dos períodos consecutivos e identifica tendencia, categorías y oportunidades explicables."
    input_schema = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}},
        "required": [],
    }

    def execute(self, *, days=30, **kwargs: Any) -> Dict[str, Any]:
        days = _days(days, 30)
        now = datetime.utcnow()
        current_start = now - timedelta(days=days)
        previous_start = current_start - timedelta(days=days)
        result = []
        for label, start, end in (("actual", current_start, now), ("anterior", previous_start, current_start)):
            query = Sale.query.filter(
                Sale.company_id == self.company_id,
                Sale.date >= start,
                Sale.date < end,
                Sale.status.notin_(["cancelada", "anulada"]),
            )
            count, total = query.with_entities(
                func.count(Sale.id),
                func.coalesce(func.sum(Sale.total_amount), 0),
            ).first()
            result.append({
                "period": label,
                "sales_count": int(count or 0),
                "sales_total": float(total or 0),
                "average_ticket": float(total or 0) / int(count or 1),
            })

        current, previous = result
        variation = None if previous["sales_total"] == 0 else round(
            ((current["sales_total"] - previous["sales_total"]) / previous["sales_total"]) * 100, 2
        )
        current_categories = _category_period(self.company_id, current_start, now)
        previous_categories = _category_period(self.company_id, previous_start, current_start)
        category_names = sorted(set(current_categories) | set(previous_categories))
        categories = []
        for name in category_names[:50]:
            current_data = current_categories.get(name, {})
            previous_data = previous_categories.get(name, {})
            current_revenue = float(current_data.get("revenue", 0))
            previous_revenue = float(previous_data.get("revenue", 0))
            categories.append({
                "category": name,
                "current_units": float(current_data.get("units", 0)),
                "previous_units": float(previous_data.get("units", 0)),
                "current_revenue": current_revenue,
                "previous_revenue": previous_revenue,
                "variation_percent": None if previous_revenue == 0 else round(((current_revenue - previous_revenue) / previous_revenue) * 100, 2),
            })
        categories.sort(key=lambda item: item["current_revenue"], reverse=True)

        opportunities = []
        if variation is not None and variation < -5:
            opportunities.append("revisar_caida_ventas")
        if variation is not None and variation > 5:
            opportunities.append("replicar_factores_de_crecimiento")
        if any(item["variation_percent"] is not None and item["variation_percent"] < -15 for item in categories):
            opportunities.append("revisar_categorias_en_caida")

        return {
            "success": True,
            "days": days,
            "periods": result,
            "variation_percent": variation,
            "trend": "creciente" if variation is not None and variation > 2 else "decreciente" if variation is not None and variation < -2 else "estable" if variation is not None else "sin_base",
            "categories": categories,
            "opportunities": opportunities,
            "data_quality": "real_db_aggregates",
        }


class ClientesInactivosTool(AgentTool):
    name = "clientes_inactivos"
    description = "Identifica clientes activos con compras históricas pero sin compras recientes, mostrando antigüedad y valor histórico para priorizar recuperación."
    input_schema = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    }

    def execute(self, *, days=90, limit=50, **kwargs: Any) -> Dict[str, Any]:
        days = _days(days, 90)
        try:
            limit = max(1, min(int(limit or 50), 100))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit debe ser un entero"}

        cutoff = datetime.utcnow() - timedelta(days=days)
        historical = Sale.query.filter(
            Sale.company_id == self.company_id,
            Sale.client_id.isnot(None),
        ).with_entities(Sale.client_id).distinct().subquery()
        recent = Sale.query.filter(
            Sale.company_id == self.company_id,
            Sale.client_id.isnot(None),
            Sale.date >= cutoff,
        ).with_entities(Sale.client_id).distinct().subquery()
        base = Client.query.filter(
            Client.company_id == self.company_id,
            Client.active.is_(True),
            Client.id.in_(historical),
            ~Client.id.in_(recent),
        )
        count = base.count()
        clients = base.order_by(Client.name.asc()).limit(limit).all()
        client_ids = [client.id for client in clients]
        history = {}
        if client_ids:
            rows = Sale.query.filter(
                Sale.company_id == self.company_id,
                Sale.client_id.in_(client_ids),
                Sale.status.notin_(["cancelada", "anulada"]),
            ).with_entities(
                Sale.client_id,
                func.max(Sale.date).label("last_purchase"),
                func.sum(Sale.total_amount).label("historical_revenue"),
                func.count(Sale.id).label("purchase_count"),
            ).group_by(Sale.client_id).all()
            history = {row.client_id: row for row in rows}

        now = datetime.utcnow()
        items = []
        for client in clients:
            row = history.get(client.id)
            last_purchase = row.last_purchase if row else None
            items.append({
                "id": client.id,
                "name": client.name,
                "email": client.email,
                "phone": client.phone,
                "last_purchase_at": last_purchase.isoformat() if last_purchase else None,
                "days_since_last_purchase": (now - last_purchase).days if last_purchase else None,
                "historical_revenue": float(row.historical_revenue or 0) if row else 0.0,
                "purchase_count": int(row.purchase_count or 0) if row else 0,
            })
        return {
            "success": True,
            "days": days,
            "count": int(count),
            "items": items,
            "priority_rule": "priorizar mayor facturación histórica y mayor antigüedad",
        }


class ProductosPromocionablesTool(AgentTool):
    name = "productos_promocionables"
    description = "Busca productos reales con stock disponible y sin ventas recientes, priorizando capital inmovilizado para preparar una propuesta comercial."
    input_schema = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": [],
    }

    def execute(self, *, days=90, limit=20, **kwargs: Any) -> Dict[str, Any]:
        days_value = _days(days, 90)
        cutoff = datetime.utcnow() - timedelta(days=days_value)
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit debe ser un entero"}
        sold = SaleItem.query.join(Sale).filter(
            Sale.company_id == self.company_id,
            Sale.date >= cutoff,
            Sale.status.notin_(["cancelada", "anulada"]),
        ).with_entities(SaleItem.product_id).distinct()
        products = Product.query.filter(
            Product.company_id == self.company_id,
            Product.active.is_(True),
            Product.stock > 0,
            ~Product.id.in_(sold),
        ).order_by(Product.stock.desc(), Product.name.asc()).limit(limit).all()
        items = []
        total_cost = 0.0
        for product in products:
            stock = float(product.stock or 0)
            cost = float(product.cost_price or 0)
            stock_cost = stock * cost
            total_cost += stock_cost
            items.append({
                "id": product.id,
                "name": product.name,
                "price": float(product.price or 0),
                "cost_price": cost,
                "stock": stock,
                "category": product.category or "Sin categoría",
                "stock_value_at_cost": stock_cost,
            })
        return {
            "success": True,
            "days": days_value,
            "items": items,
            "total_stock_value_at_cost": total_cost,
            "opportunity": "considerar promoción o liquidación priorizando mayor stock_value_at_cost",
        }


class PrepararCampanaTool(AgentTool):
    name = "preparar_campana"
    description = "Recopila contexto real de productos y clientes y prepara una campaña como BORRADOR, incluyendo señales de oportunidad, sin enviarla."
    input_schema = {
        "type": "object",
        "properties": {
            "campaign_type": {"type": "string", "enum": ["promocion_producto", "recuperacion_clientes_inactivos", "productos_sin_ventas", "general"]},
            "product_query": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
        },
        "required": ["campaign_type"],
    }

    def execute(self, *, campaign_type="general", product_query="", days=90, **kwargs: Any) -> Dict[str, Any]:
        days = _days(days, 90)
        cutoff = datetime.utcnow() - timedelta(days=days)
        product = None
        if product_query:
            like = f"%{str(product_query).strip()}%"
            product = Product.query.filter(
                Product.company_id == self.company_id,
                Product.active.is_(True),
                Product.name.ilike(like),
            ).order_by(Product.name.asc()).first()
            if product is None:
                return {"success": False, "error": "No encontré un producto real con ese nombre."}

        audience_segment = "clientes activos"
        audience_count = int(Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True)).count())
        opportunity = "promoción general"

        if campaign_type == "recuperacion_clientes_inactivos":
            historical = Sale.query.filter(
                Sale.company_id == self.company_id,
                Sale.client_id.isnot(None),
            ).with_entities(Sale.client_id).distinct().subquery()
            recent = Sale.query.filter(
                Sale.company_id == self.company_id,
                Sale.client_id.isnot(None),
                Sale.date >= cutoff,
            ).with_entities(Sale.client_id).distinct().subquery()
            audience_segment = "clientes inactivos"
            audience_count = int(Client.query.filter(
                Client.company_id == self.company_id,
                Client.active.is_(True),
                Client.id.in_(historical),
                ~Client.id.in_(recent),
            ).count())
            opportunity = "recuperación de clientes"
        elif campaign_type == "productos_sin_ventas":
            sold = SaleItem.query.join(Sale).filter(
                Sale.company_id == self.company_id,
                Sale.date >= cutoff,
                Sale.status.notin_(["cancelada", "anulada"]),
            ).with_entities(SaleItem.product_id).distinct()
            products = Product.query.filter(
                Product.company_id == self.company_id,
                Product.active.is_(True),
                Product.stock > 0,
                ~Product.id.in_(sold),
            ).order_by(Product.stock.desc()).limit(20).all()
            opportunity = "liquidar o activar productos sin movimiento"
            return {
                "success": True,
                "campaign_context": {
                    "campaign_type": campaign_type,
                    "audience_segment": audience_segment,
                    "audience_count": audience_count,
                    "products": [
                        {
                            "id": row.id,
                            "name": row.name,
                            "price": float(row.price or 0),
                            "cost_price": float(row.cost_price or 0),
                            "stock": float(row.stock or 0),
                            "stock_value_at_cost": float(row.stock or 0) * float(row.cost_price or 0),
                        }
                        for row in products
                    ],
                    "days": days,
                    "opportunity": opportunity,
                },
            }

        product_payload = None
        if product:
            product_payload = {
                "id": product.id,
                "name": product.name,
                "barcode": product.barcode,
                "price": float(product.price or 0),
                "cost_price": float(product.cost_price or 0),
                "stock": float(product.stock or 0),
                "category": product.category,
                "stock_value_at_cost": float(product.stock or 0) * float(product.cost_price or 0),
            }
        return {
            "success": True,
            "campaign_context": {
                "campaign_type": campaign_type,
                "audience_segment": audience_segment,
                "audience_count": audience_count,
                "product": product_payload,
                "days": days,
                "opportunity": opportunity,
            },
        }
