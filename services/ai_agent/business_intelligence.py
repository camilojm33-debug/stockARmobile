"""Advanced, tenant-scoped intelligence tools for StockARmobile AI agents.

These tools are read-only by default. The only mutating operation, business-memory
write, requires an explicit confirmation flag and an authenticated admin/user.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func

from app import Client, Product, Sale, SaleItem, User
from services.ai_agent.tools.base import AgentTool
from stockarmobile.extensions import db
from stockarmobile.models.ai_business_memory import AIBusinessMemory


def _days(value: Any, default: int, maximum: int = 365) -> int:
    try:
        return max(1, min(int(value or default), maximum))
    except (TypeError, ValueError):
        return default


def _since(days: Any, default: int = 30) -> datetime:
    return datetime.utcnow() - timedelta(days=_days(days, default))


def _sale_filter(query, company_id: int, start: datetime | None = None, end: datetime | None = None):
    query = query.filter(Sale.company_id == company_id, Sale.status.notin_(["cancelada", "anulada"]))
    if start is not None:
        query = query.filter(Sale.date >= start)
    if end is not None:
        query = query.filter(Sale.date < end)
    return query


class BusinessMemoryTool(AgentTool):
    name = "memoria_negocio"
    description = "Consulta la memoria empresarial estructurada del comercio: políticas, preferencias, productos estratégicos, temporadas y objetivos."
    input_schema = {
        "type": "object",
        "properties": {"category": {"type": "string"}, "key": {"type": "string"}},
        "additionalProperties": False,
    }

    def execute(self, *, category="", key="", **kwargs):
        query = AIBusinessMemory.query.filter_by(company_id=self.company_id, active=True)
        if category:
            query = query.filter_by(category=str(category).strip()[:60])
        if key:
            query = query.filter_by(memory_key=str(key).strip()[:120])
        rows = query.order_by(AIBusinessMemory.category.asc(), AIBusinessMemory.memory_key.asc()).limit(100).all()
        return {
            "success": True,
            "count": len(rows),
            "items": [
                {
                    "category": row.category,
                    "key": row.memory_key,
                    "value": row.value,
                    "source": row.source,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ],
        }


class SaveBusinessMemoryTool(AgentTool):
    name = "guardar_memoria_negocio"
    description = "Guarda una preferencia empresarial estructurada. Requiere confirm=true y un usuario autenticado; usar solo para instrucciones explícitas del responsable del comercio."
    input_schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "minLength": 1, "maxLength": 60},
            "key": {"type": "string", "minLength": 1, "maxLength": 120},
            "value": {"type": "string", "minLength": 1, "maxLength": 1000},
            "confirm": {"type": "boolean"},
        },
        "required": ["category", "key", "value", "confirm"],
        "additionalProperties": False,
    }

    def execute(self, *, category, key, value, confirm=False, **kwargs):
        actor_user_id = self._context.get("actor_user_id")
        if not confirm or not actor_user_id:
            return {"success": False, "error": "confirmation_required"}
        actor = User.query.filter_by(id=actor_user_id, company_id=self.company_id, active=True).first()
        if actor is None:
            return {"success": False, "error": "authenticated_user_required"}
        row = AIBusinessMemory.query.filter_by(
            company_id=self.company_id,
            category=str(category).strip()[:60],
            memory_key=str(key).strip()[:120],
        ).first()
        if row is None:
            row = AIBusinessMemory(
                company_id=self.company_id,
                category=str(category).strip()[:60],
                memory_key=str(key).strip()[:120],
                created_by_user_id=actor.id,
            )
            db.session.add(row)
        row.value = str(value).strip()[:1000]
        row.source = "ai_confirmed"
        row.active = True
        db.session.commit()
        return {"success": True, "category": row.category, "key": row.memory_key, "saved": True}


class ProductRecommendationsTool(AgentTool):
    name = "recomendar_productos"
    description = "Recomienda productos reales por categoría, stock, precio y ventas recientes, útil para venta cruzada y sustitutos."
    input_schema = {
        "type": "object",
        "properties": {
            "product_query": {"type": "string"},
            "category": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    }

    def execute(self, *, product_query="", category="", limit=8, **kwargs):
        try:
            limit = max(1, min(int(limit or 8), 20))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit_invalid"}
        query = Product.query.filter(Product.company_id == self.company_id, Product.active.is_(True))
        if category:
            query = query.filter(Product.category.ilike(f"%{str(category).strip()}%"))
        if product_query:
            like = f"%{str(product_query).strip()}%"
            candidates = query.filter(Product.name.ilike(like)).order_by(Product.stock.desc(), Product.name.asc()).limit(limit).all()
        else:
            candidates = query.order_by(Product.stock.desc(), Product.name.asc()).limit(limit).all()
        return {
            "success": True,
            "items": [
                {"id": p.id, "name": p.name, "category": p.category, "price": float(p.price or 0), "stock": float(p.stock or 0)}
                for p in candidates
            ],
        }


class ProductProfitabilityTool(AgentTool):
    name = "rentabilidad_productos"
    description = "Calcula margen unitario y margen monetario sobre ventas reales del período, sin inventar costos."
    input_schema = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "additionalProperties": False,
    }

    def execute(self, *, days=30, limit=20, **kwargs):
        days = _days(days, 30)
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit_invalid"}
        start = _since(days, days)
        rows = SaleItem.query.join(Sale).join(Product).filter(
            Sale.company_id == self.company_id,
            Sale.date >= start,
            Sale.status.notin_(["cancelada", "anulada"]),
            Product.company_id == self.company_id,
        ).with_entities(
            Product.id,
            Product.name,
            Product.category,
            func.coalesce(func.sum(SaleItem.quantity), 0).label("units"),
            func.coalesce(func.sum(SaleItem.quantity * SaleItem.price), 0).label("revenue"),
            func.coalesce(func.avg(Product.cost_price), 0).label("cost"),
        ).group_by(Product.id, Product.name, Product.category).order_by(func.sum(SaleItem.quantity * SaleItem.price).desc()).limit(limit).all()
        items = []
        for row in rows:
            cost = float(row.cost or 0)
            units = float(row.units or 0)
            revenue = float(row.revenue or 0)
            total_cost = cost * units
            margin = revenue - total_cost
            margin_pct = None if revenue == 0 else round(margin / revenue * 100, 2)
            items.append({"id": row.id, "name": row.name, "category": row.category, "units": units, "revenue": revenue, "estimated_cost": total_cost, "estimated_margin": margin, "margin_percent": margin_pct})
        return {"success": True, "days": days, "items": items, "data_quality": "real_sales_and_product_costs"}


class InventoryTurnoverTool(AgentTool):
    name = "rotacion_inventario"
    description = "Estima rotación usando unidades vendidas del período y stock actual; entrega datos, no pronósticos."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 7, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "additionalProperties": False}

    def execute(self, *, days=30, limit=20, **kwargs):
        days = _days(days, 30)
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit_invalid"}
        start = _since(days, days)
        sold = SaleItem.query.join(Sale).filter(
            Sale.company_id == self.company_id,
            Sale.date >= start,
            Sale.status.notin_(["cancelada", "anulada"]),
        ).with_entities(SaleItem.product_id, func.coalesce(func.sum(SaleItem.quantity), 0).label("units")).group_by(SaleItem.product_id).subquery()
        rows = Product.query.outerjoin(sold, Product.id == sold.c.product_id).filter(Product.company_id == self.company_id, Product.active.is_(True)).with_entities(Product.id, Product.name, Product.stock, func.coalesce(sold.c.units, 0).label("units_sold")).order_by(func.coalesce(sold.c.units, 0).desc()).limit(limit).all()
        items = []
        for row in rows:
            stock = float(row.stock or 0)
            sold_units = float(row.units_sold or 0)
            turnover = None if stock <= 0 else round(sold_units / stock, 4)
            items.append({"id": row.id, "name": row.name, "stock": stock, "units_sold": sold_units, "turnover_ratio": turnover})
        return {"success": True, "days": days, "items": items, "interpretation": "rotacion_ratio = unidades vendidas del período / stock actual"}


class SalesAnomalyTool(AgentTool):
    name = "anomalías_ventas"
    description = "Detecta desviaciones simples comparando semanas recientes contra una línea base histórica; no atribuye causas sin evidencia."
    input_schema = {"type": "object", "properties": {"weeks": {"type": "integer", "minimum": 4, "maximum": 26}}, "additionalProperties": False}

    def execute(self, *, weeks=8, **kwargs):
        weeks = _days(weeks, 8, 26)
        now = datetime.utcnow()
        weekly = []
        for index in range(weeks):
            end = now - timedelta(days=index * 7)
            start = end - timedelta(days=7)
            count, total = _sale_filter(Sale.query, self.company_id, start, end).with_entities(func.count(Sale.id), func.coalesce(func.sum(Sale.total_amount), 0)).first()
            weekly.append(float(total or 0))
        current = weekly[0] if weekly else 0.0
        baseline = sum(weekly[1:]) / max(1, len(weekly) - 1)
        deviation = None if baseline == 0 else round((current - baseline) / baseline * 100, 2)
        return {"success": True, "weeks": weeks, "current_week_sales": current, "historical_weekly_average": round(baseline, 2), "deviation_percent": deviation, "signal": "caida" if deviation is not None and deviation < -20 else "suba" if deviation is not None and deviation > 20 else "normal"}


class ExecutiveSummaryTool(AgentTool):
    name = "resumen_ejecutivo"
    description = "Construye un resumen ejecutivo con ventas, stock crítico, clientes inactivos y oportunidades actuales usando datos reales."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 7, "maximum": 365}}, "additionalProperties": False}

    def execute(self, *, days=30, **kwargs):
        days = _days(days, 30)
        start = _since(days, days)
        count, total = _sale_filter(Sale.query, self.company_id, start, datetime.utcnow()).with_entities(func.count(Sale.id), func.coalesce(func.sum(Sale.total_amount), 0)).first()
        critical = Product.query.filter(Product.company_id == self.company_id, Product.active.is_(True), Product.stock <= 0).count()
        historical = Sale.query.filter(Sale.company_id == self.company_id, Sale.client_id.isnot(None)).with_entities(Sale.client_id).distinct().subquery()
        recent = Sale.query.filter(Sale.company_id == self.company_id, Sale.client_id.isnot(None), Sale.date >= start).with_entities(Sale.client_id).distinct().subquery()
        inactive = Client.query.filter(Client.company_id == self.company_id, Client.active.is_(True), Client.id.in_(historical), ~Client.id.in_(recent)).count()
        return {"success": True, "days": days, "sales_count": int(count or 0), "sales_total": float(total or 0), "critical_stock_products": int(critical), "inactive_clients": int(inactive), "signals": ["stock_critico" if critical else None, "clientes_inactivos" if inactive else None]}


class CustomerSegmentsTool(AgentTool):
    name = "segmentar_clientes"
    description = "Segmenta clientes reales por actividad de compra: frecuentes, inactivos y alto valor."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 30, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}

    def execute(self, *, days=90, limit=50, **kwargs):
        days = _days(days, 90)
        try:
            limit = max(1, min(int(limit or 50), 100))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit_invalid"}
        start = _since(days, days)
        rows = _sale_filter(Sale.query, self.company_id).filter(Sale.client_id.isnot(None)).with_entities(Sale.client_id, func.count(Sale.id).label("orders"), func.sum(Sale.total_amount).label("revenue"), func.max(Sale.date).label("last_purchase")).group_by(Sale.client_id).order_by(func.sum(Sale.total_amount).desc()).limit(limit).all()
        items = []
        for row in rows:
            last = row.last_purchase
            days_since = (datetime.utcnow() - last).days if last else None
            segment = "alto_valor" if float(row.revenue or 0) > 0 and float(row.revenue or 0) >= max(1.0, float(sum(float(r.revenue or 0) for r in rows)) / max(1, len(rows)) * 2) else "frecuente" if int(row.orders or 0) >= 3 else "activo"
            if days_since is not None and days_since >= days:
                segment = "inactivo"
            items.append({"client_id": row.client_id, "orders": int(row.orders or 0), "revenue": float(row.revenue or 0), "days_since_last_purchase": days_since, "segment": segment})
        return {"success": True, "days": days, "items": items}


class BusinessAlertsTool(AgentTool):
    name = "alertas_negocio"
    description = "Detecta alertas operativas y comerciales actuales para presentar al responsable, sin ejecutar acciones."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs):
        summary = ExecutiveSummaryTool(company_id=self.company_id, **self._context).execute(days=30)
        anomalies = SalesAnomalyTool(company_id=self.company_id, **self._context).execute(weeks=8)
        alerts = []
        if summary.get("critical_stock_products", 0):
            alerts.append({"type": "stock", "severity": "high", "count": summary["critical_stock_products"]})
        if summary.get("inactive_clients", 0):
            alerts.append({"type": "clients", "severity": "medium", "count": summary["inactive_clients"]})
        if anomalies.get("signal") != "normal":
            alerts.append({"type": "sales", "severity": "high", "signal": anomalies.get("signal"), "deviation_percent": anomalies.get("deviation_percent")})
        return {"success": True, "alerts": alerts, "summary": summary, "sales_signal": anomalies}


class SharedOpportunitiesTool(AgentTool):
    name = "oportunidades_ia"
    description = "Une señales de ventas, stock, clientes y productos promocionables para que los agentes coordinen oportunidades."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs):
        summary = ExecutiveSummaryTool(company_id=self.company_id, **self._context).execute(days=30)
        anomalies = SalesAnomalyTool(company_id=self.company_id, **self._context).execute(weeks=8)
        profit = ProductProfitabilityTool(company_id=self.company_id, **self._context).execute(days=30, limit=10)
        opportunities = []
        if summary.get("inactive_clients", 0):
            opportunities.append({"source": "analista", "type": "recuperacion_clientes", "count": summary["inactive_clients"]})
        if summary.get("critical_stock_products", 0):
            opportunities.append({"source": "asistente", "type": "reponer_stock", "count": summary["critical_stock_products"]})
        if anomalies.get("signal") == "caida":
            opportunities.append({"source": "analista", "type": "revisar_caida_ventas", "deviation_percent": anomalies.get("deviation_percent")})
        if profit.get("items"):
            low_margin = [row for row in profit["items"] if row.get("margin_percent") is not None and row["margin_percent"] < 15]
            if low_margin:
                opportunities.append({"source": "analista", "type": "revisar_margen", "products": low_margin[:5]})
        return {"success": True, "opportunities": opportunities}


def install_ai_intelligence_tools(runtime_cls) -> None:
    tools = {
        "memoria_negocio": BusinessMemoryTool,
        "guardar_memoria_negocio": SaveBusinessMemoryTool,
        "recomendar_productos": ProductRecommendationsTool,
        "rentabilidad_productos": ProductProfitabilityTool,
        "rotacion_inventario": InventoryTurnoverTool,
        "anomalías_ventas": SalesAnomalyTool,
        "resumen_ejecutivo": ExecutiveSummaryTool,
        "segmentar_clientes": CustomerSegmentsTool,
        "alertas_negocio": BusinessAlertsTool,
        "oportunidades_ia": SharedOpportunitiesTool,
    }
    runtime_cls.tool_registry.update(tools)
    runtime_cls.agent_tool_names.setdefault("vendedor", set()).update({"memoria_negocio", "recomendar_productos", "oportunidades_ia"})
    runtime_cls.agent_tool_names.setdefault("asistente", set()).update({"memoria_negocio", "guardar_memoria_negocio", "resumen_ejecutivo", "alertas_negocio", "oportunidades_ia", "rentabilidad_productos", "rotacion_inventario", "segmentar_clientes"})
    runtime_cls.agent_tool_names.setdefault("analista", set()).update({"memoria_negocio", "guardar_memoria_negocio", "rentabilidad_productos", "rotacion_inventario", "anomalías_ventas", "resumen_ejecutivo", "segmentar_clientes", "alertas_negocio", "oportunidades_ia"})
    runtime_cls.agent_tool_names.setdefault("marketing", set()).update({"memoria_negocio", "segmentar_clientes", "rentabilidad_productos", "oportunidades_ia"})
