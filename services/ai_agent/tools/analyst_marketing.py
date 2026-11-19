"""Tenant-scoped read-only tools for analyst and marketing conversations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func

from app import Client, Product, Sale, SaleItem, db
from services.ai_agent.tools.base import AgentTool


def _days(value: Any, default: int) -> int:
    try:
        return max(1, min(int(value or default), 365))
    except (TypeError, ValueError):
        return default


def _date_limit(days: Any, default: int) -> datetime:
    return datetime.utcnow() - timedelta(days=_days(days, default))


def _scope_sale_query(query, company_id: int):
    from services.ai_agent.tools.branch_scope import employee_branch_scope
    restricted, branch_id = employee_branch_scope(company_id)
    return query.filter(Sale.branch_id == branch_id) if restricted else query


def _product_stock(product, company_id: int) -> float:
    from services.ai_agent.tools.branch_scope import employee_branch_scope
    restricted, branch_id = employee_branch_scope(company_id)
    if not restricted:
        return float(product.stock or 0)
    from stockarmobile.branch_inventory import available_stock
    return available_stock(product, branch_id) if branch_id is not None else 0.0


def _campaign_audience_counts(company_id: int, campaign_type: str, cutoff: datetime) -> dict:
    """Count only active WhatsApp contacts with documented marketing opt-in."""
    from services.ai_agent.tenant_campaign_service import _client_phone

    audience_query = Client.query.filter(
        Client.company_id == int(company_id),
        Client.active.is_(True),
    )
    if campaign_type == "recuperacion_clientes_inactivos":
        historical = _scope_sale_query(Sale.query.filter(
            Sale.company_id == int(company_id),
            Sale.client_id.isnot(None),
            Sale.status.notin_(["cancelada", "anulada"]),
        ), company_id).with_entities(Sale.client_id).distinct().subquery()
        recent = _scope_sale_query(Sale.query.filter(
            Sale.company_id == int(company_id),
            Sale.client_id.isnot(None),
            Sale.date >= cutoff,
            Sale.status.notin_(["cancelada", "anulada"]),
        ), company_id).with_entities(Sale.client_id).distinct().subquery()
        audience_query = audience_query.filter(
            Client.id.in_(historical),
            ~Client.id.in_(recent),
        )

    audience_total = audience_query.count()
    eligible_query = audience_query.filter(
        Client.whatsapp.isnot(None),
        func.length(func.trim(Client.whatsapp)) > 0,
        Client.whatsapp_marketing_consent == "opted_in",
        Client.whatsapp_marketing_consent_recorded_at.isnot(None),
        func.length(func.trim(Client.whatsapp_marketing_consent_source)) > 0,
        func.length(func.trim(Client.whatsapp_marketing_consent_evidence)) > 0,
    )
    eligible = sum(1 for client in eligible_query.yield_per(500) if _client_phone(client))
    return {
        "audience_count": eligible,
        "audience_total_count": audience_total,
        "audience_excluded_count": max(0, audience_total - eligible),
    }

def _branch_stock_products(company_id: int, *, limit: int):
    from services.ai_agent.tools.branch_scope import employee_branch_scope
    restricted, branch_id = employee_branch_scope(company_id)
    query = Product.query.filter(Product.company_id == int(company_id), Product.active.is_(True))
    if restricted:
        from app import BranchStock
        rows = query.join(
            BranchStock,
            (BranchStock.company_id == Product.company_id)
            & (BranchStock.product_id == Product.id)
            & (BranchStock.branch_id == branch_id),
        ).filter(BranchStock.stock > 0).order_by(BranchStock.stock.desc(), Product.name.asc()).limit(limit).all()
        for product in rows:
            product._branch_stock_for_ai = _product_stock(product, company_id)
        return rows
    return query.filter(Product.stock > 0).order_by(Product.stock.desc(), Product.name.asc()).limit(limit).all()


def _category_period(company_id: int, start: datetime, end: datetime) -> Dict[str, Dict[str, float]]:
    category_query = SaleItem.query.join(Sale).join(Product).filter(
        Sale.company_id == company_id,
        Sale.date >= start,
        Sale.date < end,
        Sale.status.notin_(["cancelada", "anulada"]),
        Product.company_id == company_id,
    )
    category_query = _scope_sale_query(category_query, company_id)
    rows = category_query.with_entities(
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
            query = _scope_sale_query(Sale.query.filter(
                Sale.company_id == self.company_id,
                Sale.date >= start,
                Sale.date < end,
                Sale.status.notin_(["cancelada", "anulada"]),
            ), self.company_id)
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
        # Only completed/non-voided sales should establish history or recent activity.
        # Aggregate history once and rank eligible inactive customers by value and recency,
        # matching the priority rule returned to the agent.
        valid_sales = _scope_sale_query(Sale.query.filter(
            Sale.company_id == self.company_id,
            Sale.client_id.isnot(None),
            Sale.status.notin_(["cancelada", "anulada"]),
        ), self.company_id)
        history = valid_sales.with_entities(
            Sale.client_id.label("client_id"),
            func.max(Sale.date).label("last_purchase"),
            func.sum(Sale.total_amount).label("historical_revenue"),
            func.count(Sale.id).label("purchase_count"),
        ).group_by(Sale.client_id).subquery()
        recent = _scope_sale_query(Sale.query.filter(
            Sale.company_id == self.company_id,
            Sale.client_id.isnot(None),
            Sale.status.notin_(["cancelada", "anulada"]),
            Sale.date >= cutoff,
        ), self.company_id).with_entities(Sale.client_id).distinct().subquery()
        base = (
            db.session.query(
                Client,
                history.c.last_purchase,
                history.c.historical_revenue,
                history.c.purchase_count,
            )
            .join(history, history.c.client_id == Client.id)
            .filter(
                Client.company_id == self.company_id,
                Client.active.is_(True),
                ~Client.id.in_(recent),
            )
        )
        count = base.count()
        rows = base.order_by(
            history.c.historical_revenue.desc(),
            history.c.last_purchase.asc(),
            Client.name.asc(),
        ).limit(limit).all()

        now = datetime.utcnow()
        items = []
        for client, last_purchase, historical_revenue, purchase_count in rows:
            items.append({
                "id": client.id,
                "name": client.name,
                "phone": client.phone,
                "last_purchase_at": last_purchase.isoformat() if last_purchase else None,
                "days_since_last_purchase": (now - last_purchase).days if last_purchase else None,
                "historical_revenue": float(historical_revenue or 0),
                "purchase_count": int(purchase_count or 0),
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
        sold = _scope_sale_query(SaleItem.query.join(Sale).filter(
            Sale.company_id == self.company_id,
            Sale.date >= cutoff,
            Sale.status.notin_(["cancelada", "anulada"]),
        ), self.company_id).with_entities(SaleItem.product_id).distinct()
        products = [
            product for product in _branch_stock_products(self.company_id, limit=1000)
            if product.id not in {int(row[0]) for row in sold.all()}
        ][:limit]
        products.sort(key=lambda product: _product_stock(product, self.company_id), reverse=True)
        items = []
        total_cost = 0.0
        for product in products:
            stock = _product_stock(product, self.company_id)
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


class OportunidadesMarketingTool(AgentTool):
    name = "oportunidades_marketing"
    description = "Detecta oportunidades comerciales reales para Marketing IA combinando clientes inactivos y productos con stock sin ventas recientes."
    input_schema = {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": []}

    def execute(self, *, days=90, limit=20, **kwargs: Any) -> Dict[str, Any]:
        days = _days(days, 90)
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            limit = 20
        inactive = ClientesInactivosTool(company_id=self.company_id).execute(days=days, limit=limit)
        products = ProductosPromocionablesTool(company_id=self.company_id).execute(days=days, limit=limit)
        opportunities = []
        for item in inactive.get("items", [])[:limit]:
            opportunities.append({"type": "recuperacion_cliente", "client_id": item["id"], "client_name": item["name"], "evidence": {"days_since_last_purchase": item["days_since_last_purchase"], "historical_revenue": item["historical_revenue"], "purchase_count": item["purchase_count"]}})
        for item in products.get("items", [])[:limit]:
            opportunities.append({"type": "producto_sin_movimiento", "product_id": item["id"], "product_name": item["name"], "evidence": {"stock": item["stock"], "stock_value_at_cost": item["stock_value_at_cost"], "days_without_sales": days}})
        return {"success": True, "days": days, "count": len(opportunities[:limit]), "opportunities": opportunities[:limit], "data_quality": "real_db_aggregates"}


class PrepararCampanaTool(AgentTool):
    name = "preparar_campana"
    description = (
        "Analiza productos y clientes reales, cuenta solo contactos con WhatsApp y consentimiento "
        "promocional documentado, y prepara una campaña como BORRADOR sin enviarla. "
        "Cuando la campaña mencione el precio de un producto, no escribas un importe fijo ni uses el precio minorista como si fuera universal: "
        "incluí la variable {{precio}}. El backend la resolverá para cada destinatario con su propia lista mayorista, cantidad y vigencia. "
        "Si el usuario indica una cantidad de compra, informala en el campo pricing_quantity. "
        "Redactá el texto exacto que recibirán los clientes y pasalo en message; no devuelvas una explicación como mensaje."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "campaign_type": {"type": "string", "enum": ["promocion_producto", "recuperacion_clientes_inactivos", "productos_sin_ventas", "general"]},
            "title": {"type": "string", "description": "Título breve y claro para identificar el borrador de campaña."},
            "objective": {"type": "string", "description": "Objetivo comercial concreto de la campaña."},
            "message": {"type": "string", "maxLength": 820, "description": "Texto exacto para WhatsApp, listo para enviar. En español argentino, breve, claro, con llamada a la acción. No inventes precios, descuentos, stock ni condiciones; usá solo los datos comprobados. No incluyas instrucciones técnicas ni expliques el análisis."},
            "product_query": {"type": "string"},
            "pricing_quantity": {"type": "number", "minimum": 1, "description": "Cantidad de unidades sobre la que se debe resolver el precio. Si se omite, se usa el primer tramo vigente de la lista de cada cliente mayorista."},
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
        },
        "required": ["campaign_type", "message"],
    }

    def execute(self, *, campaign_type="general", title="", objective="", message="", product_query="", pricing_quantity=None, days=90, channel="whatsapp", **kwargs: Any) -> Dict[str, Any]:
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
            if product is not None:
                product._branch_stock_for_ai = _product_stock(product, self.company_id)
            if product is None:
                return {"success": False, "error": "No encontré un producto real con ese nombre."}

        audience_segment = "clientes activos"
        audience_counts = _campaign_audience_counts(self.company_id, campaign_type, cutoff)
        opportunity = "promoción general"
        campaign_title = str(title or "").strip()[:180]
        campaign_objective = str(objective or "").strip()[:120]
        campaign_message = str(message or "").strip()[:820]
        try:
            parsed_pricing_quantity = float(pricing_quantity) if pricing_quantity not in (None, "") else None
        except (TypeError, ValueError):
            return {"success": False, "error": "La cantidad para resolver el precio no es válida."}
        if parsed_pricing_quantity is not None and parsed_pricing_quantity <= 0:
            return {"success": False, "error": "La cantidad para resolver el precio debe ser mayor a cero."}
        if not campaign_message:
            return {"success": False, "error": "La campaña necesita un texto concreto para WhatsApp. Redactá el mensaje dentro de la propuesta."}
        try:
            from stockarmobile.branch_context import get_active_branch_id
            active_branch_id = get_active_branch_id()
        except Exception:
            active_branch_id = None

        if campaign_type == "recuperacion_clientes_inactivos":
            audience_segment = "clientes inactivos"
            opportunity = "recuperación de clientes"
        elif campaign_type == "productos_sin_ventas":
            sold = _scope_sale_query(SaleItem.query.join(Sale).filter(
                Sale.company_id == self.company_id,
                Sale.date >= cutoff,
                Sale.status.notin_(["cancelada", "anulada"]),
            ), self.company_id).with_entities(SaleItem.product_id).distinct()
            sold_ids = {int(row[0]) for row in sold.all()}
            products = [
                product for product in _branch_stock_products(self.company_id, limit=1000)
                if product.id not in sold_ids
            ][:20]
            products.sort(key=lambda product: _product_stock(product, self.company_id), reverse=True)
            opportunity = "liquidar o activar productos sin movimiento"
            return {
                "success": True,
                "campaign_context": {
                    "campaign_type": campaign_type,
                    "channel": "whatsapp",
                    "branch_id": active_branch_id,
                    "title": campaign_title or "Productos sin movimiento",
                    "objective": campaign_objective or opportunity,
                    "message": campaign_message,
                    "audience_segment": audience_segment,
                    **audience_counts,
                    "products": [
                        {
                            "id": row.id,
                            "name": row.name,
                            "price": float(row.price or 0),
                            "cost_price": float(row.cost_price or 0),
                            "stock": _product_stock(row, self.company_id),
                            "stock_value_at_cost": _product_stock(row, self.company_id) * float(row.cost_price or 0),
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
                "stock": _product_stock(product, self.company_id),
                "category": product.category,
                "stock_value_at_cost": _product_stock(product, self.company_id) * float(product.cost_price or 0),
            }
            copy_mentions_price = bool(re.search(
                r"\bprecio\b|\bmayorista\b|\bpor mayor\b",
                campaign_objective + " " + campaign_message + " " + campaign_title,
                re.IGNORECASE,
            ))
            if copy_mentions_price and "{{precio}}" not in campaign_message:
                # Preserve recipient-level pricing instead of freezing the retail
                # amount generated by the model into the campaign copy.
                campaign_message = f"{campaign_message.rstrip()}\nPrecio por unidad según tu lista: {{{{precio}}}}."
                campaign_message = campaign_message[:820]
        return {
            "success": True,
            "campaign_context": {
                "campaign_type": campaign_type,
                "channel": "whatsapp",
                "branch_id": active_branch_id,
                "title": campaign_title or (product_payload.get("name") if product_payload else "Campaña para clientes"),
                "objective": campaign_objective or opportunity,
                "message": campaign_message,
                "audience_segment": audience_segment,
                **audience_counts,
                "product": product_payload,
                "pricing_quantity": parsed_pricing_quantity,
                "days": days,
                "opportunity": opportunity,
            },
        }
