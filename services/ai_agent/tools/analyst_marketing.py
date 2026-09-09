"""Tenant-scoped read-only tools for analyst and marketing conversations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func

from app import Client, Product, Sale, SaleItem
from services.ai_agent.tools.base import AgentTool


def _date_limit(days: Any, default: int) -> datetime:
    try:
        value = max(1, min(int(days or default), 365))
    except (TypeError, ValueError):
        value = default
    return datetime.utcnow() - timedelta(days=value)


class VentasComparativaTool(AgentTool):
    name = "comparar_ventas"
    description = "Compara ventas reales de dos períodos consecutivos y devuelve datos para análisis explicable."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}}, "required": []}

    def execute(self, *, days=30, **kwargs: Any) -> Dict[str, Any]:
        try:
            days = max(1, min(int(days or 30), 365))
        except (TypeError, ValueError):
            return {"success": False, "error": "days debe ser un entero"}
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
            count, total = query.with_entities(func.count(Sale.id), func.coalesce(func.sum(Sale.total_amount), 0)).first()
            result.append({"period": label, "sales_count": int(count or 0), "sales_total": float(total or 0), "average_ticket": float(total or 0) / int(count or 1)})
        current, previous = result
        variation = None if previous["sales_total"] == 0 else round(((current["sales_total"] - previous["sales_total"]) / previous["sales_total"]) * 100, 2)
        return {"success": True, "days": days, "periods": result, "variation_percent": variation}


class ClientesInactivosTool(AgentTool):
    name = "clientes_inactivos"
    description = "Identifica clientes activos con compras históricas pero sin compras recientes, dentro de la empresa."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": []}

    def execute(self, *, days=90, limit=50, **kwargs: Any) -> Dict[str, Any]:
        try:
            days = max(1, min(int(days or 90), 365))
            limit = max(1, min(int(limit or 50), 100))
        except (TypeError, ValueError):
            return {"success": False, "error": "days y limit deben ser enteros"}
        cutoff = datetime.utcnow() - timedelta(days=days)
        historical = Sale.query.filter(Sale.company_id == self.company_id, Sale.client_id.isnot(None)).with_entities(Sale.client_id).distinct().subquery()
        recent = Sale.query.filter(Sale.company_id == self.company_id, Sale.client_id.isnot(None), Sale.date >= cutoff).with_entities(Sale.client_id).distinct().subquery()
        query = Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True), Client.id.in_(historical), ~Client.id.in_(recent)).order_by(Client.name.asc())
        clients = query.limit(limit).all()
        count = query.count()
        return {"success": True, "days": days, "count": int(count), "items": [{"id": client.id, "name": client.name, "email": client.email, "phone": client.phone} for client in clients]}


class ProductosPromocionablesTool(AgentTool):
    name = "productos_promocionables"
    description = "Busca productos reales con stock disponible y sin ventas recientes para preparar una propuesta comercial."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": []}

    def execute(self, *, days=90, limit=20, **kwargs: Any) -> Dict[str, Any]:
        cutoff = _date_limit(days, 90)
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit debe ser un entero"}
        sold = SaleItem.query.join(Sale).filter(Sale.company_id == self.company_id, Sale.date >= cutoff, Sale.status.notin_(["cancelada", "anulada"])).with_entities(SaleItem.product_id).distinct()
        products = Product.query.filter(Product.company_id == self.company_id, Product.active.is_(True), Product.stock > 0, ~Product.id.in_(sold)).order_by(Product.stock.desc(), Product.name.asc()).limit(limit).all()
        return {"success": True, "items": [{"id": product.id, "name": product.name, "price": float(product.price or 0), "stock": float(product.stock or 0), "category": product.category} for product in products]}


class PrepararCampanaTool(AgentTool):
    name = "preparar_campana"
    description = "Recopila datos reales para que Marketing IA genere y guarde una campaña como borrador, sin enviarla."
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
        try:
            days = max(1, min(int(days or 90), 365))
        except (TypeError, ValueError):
            return {"success": False, "error": "days debe ser un entero"}
        cutoff = datetime.utcnow() - timedelta(days=days)
        product = None
        if product_query:
            like = f"%{str(product_query).strip()}%"
            product = Product.query.filter(Product.company_id == self.company_id, Product.active.is_(True), Product.name.ilike(like)).order_by(Product.name.asc()).first()
            if product is None:
                return {"success": False, "error": "No encontré un producto real con ese nombre."}
        audience_segment = "clientes activos"
        audience_count = int(Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True)).count())
        if campaign_type == "recuperacion_clientes_inactivos":
            historical = Sale.query.filter(Sale.company_id == self.company_id, Sale.client_id.isnot(None)).with_entities(Sale.client_id).distinct().subquery()
            recent = Sale.query.filter(Sale.company_id == self.company_id, Sale.client_id.isnot(None), Sale.date >= cutoff).with_entities(Sale.client_id).distinct().subquery()
            audience_segment = "clientes inactivos"
            audience_count = int(Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True), Client.id.in_(historical), ~Client.id.in_(recent)).count())
        elif campaign_type == "productos_sin_ventas":
            audience_segment = "clientes activos"
            sold = SaleItem.query.join(Sale).filter(Sale.company_id == self.company_id, Sale.date >= cutoff, Sale.status.notin_(["cancelada", "anulada"])).with_entities(SaleItem.product_id).distinct()
            products = Product.query.filter(Product.company_id == self.company_id, Product.active.is_(True), Product.stock > 0, ~Product.id.in_(sold)).limit(20).all()
            return {"success": True, "campaign_context": {"campaign_type": campaign_type, "audience_segment": audience_segment, "audience_count": audience_count, "products": [{"id": row.id, "name": row.name, "price": float(row.price or 0), "stock": float(row.stock or 0)} for row in products], "days": days}}
        return {"success": True, "campaign_context": {"campaign_type": campaign_type, "audience_segment": audience_segment, "audience_count": audience_count, "product": ({"id": product.id, "name": product.name, "barcode": product.barcode, "price": float(product.price or 0), "stock": float(product.stock or 0), "category": product.category} if product else None), "days": days}}
