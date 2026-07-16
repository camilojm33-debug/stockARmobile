"""Consultas agregadas para el dashboard."""

import json
from decimal import Decimal
from datetime import datetime, timedelta, time

from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload
from services.sales_calculation_service import sale_payment_breakdown, to_decimal


def build_dashboard_context():
    from app import CashSession, Client, Expense, Product, Sale, SaleItem, db, scope_query_to_company, utcnow

    now = utcnow()
    today_start = datetime.combine(now.date(), time.min)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = datetime(now.year, now.month, 1)

    total_products = scope_query_to_company(Product.query.filter_by(active=True), Product).count()
    low_stock_count = scope_query_to_company(Product.query.filter(Product.active.is_(True), Product.stock <= Product.min_stock), Product).count()
    out_stock_count = scope_query_to_company(Product.query.filter(Product.active.is_(True), Product.stock <= 0), Product).count()
    total_clients = scope_query_to_company(Client.query.filter_by(active=True), Client).count()
    new_clients_month = scope_query_to_company(Client.query.filter(Client.active.is_(True), Client.created_at >= month_start), Client).count()

    can_view_economic_metrics = _can_view_economic_metrics()

    confirmed_sales_base = _confirmed_sales_query(Sale.query, Sale)
    total_sales_amount = _sum(Sale.total_amount, model=Sale, base_query=confirmed_sales_base) if can_view_economic_metrics else Decimal("0.00")
    sales_today = confirmed_sales_base.filter(Sale.date >= today_start).count()
    sales_week = confirmed_sales_base.filter(Sale.date >= week_start).count()
    sales_month = confirmed_sales_base.filter(Sale.date >= month_start).count()
    income_today = _sum(Sale.total_amount, Sale.date >= today_start, model=Sale, base_query=confirmed_sales_base) if can_view_economic_metrics else None
    income_week = _sum(Sale.total_amount, Sale.date >= week_start, model=Sale, base_query=confirmed_sales_base) if can_view_economic_metrics else None
    income_month = _sum(Sale.total_amount, Sale.date >= month_start, model=Sale, base_query=confirmed_sales_base) if can_view_economic_metrics else None
    expenses_today = _sum(Expense.amount, Expense.date >= today_start, model=Expense) if can_view_economic_metrics else Decimal("0.00")
    expenses_month = _sum(Expense.amount, Expense.date >= month_start, model=Expense) if can_view_economic_metrics else Decimal("0.00")
    cost_today = _sum_item_cost(Sale.date >= today_start) if can_view_economic_metrics else Decimal("0.00")
    cost_month = _sum_item_cost(Sale.date >= month_start) if can_view_economic_metrics else Decimal("0.00")

    profit_today = (income_today - cost_today - expenses_today) if can_view_economic_metrics else None
    profit_month = (income_month - cost_month - expenses_month) if can_view_economic_metrics else None
    total_sales_count = confirmed_sales_base.count()
    average_ticket = ((total_sales_amount / Decimal(total_sales_count)) if total_sales_count else Decimal("0.00")) if can_view_economic_metrics else None
    sold_units = _sum_item_quantity() if can_view_economic_metrics else 0

    top_products = []
    if can_view_economic_metrics:
        top_products_query = db.session.query(
                Product.id.label("prod_id"),
                Product.name.label("name"),
                db.func.coalesce(db.func.sum(SaleItem.quantity), 0).label("sales_count"),
                db.func.coalesce(db.func.sum(SaleItem.quantity * SaleItem.price), 0).label("total_sales"),
            )
        top_products = scope_query_to_company(
            top_products_query
            .join(SaleItem, Product.id == SaleItem.product_id)
            .join(Sale, SaleItem.sale_id == Sale.id)
            .filter(_confirmed_sale_status_expression(Sale))
            .group_by(Product.id, Product.name)
            .order_by(db.desc("sales_count")),
            Product,
        ).limit(10).all()
    least_products_query = db.session.query(
            Product.id.label("prod_id"),
            Product.name.label("name"),
            db.func.coalesce(db.func.sum(SaleItem.quantity), 0).label("sales_count"),
        )
    least_products = scope_query_to_company(
        least_products_query
        .outerjoin(SaleItem, Product.id == SaleItem.product_id)
        .outerjoin(Sale, SaleItem.sale_id == Sale.id)
        .filter(or_(Sale.id.is_(None), _confirmed_sale_status_expression(Sale)))
        .filter(Product.active.is_(True))
        .group_by(Product.id, Product.name)
        .order_by(db.asc("sales_count")),
        Product,
    ).limit(5).all()
    ranking_clients = []
    if can_view_economic_metrics:
        ranking_clients_query = db.session.query(
                Client.id.label("id"),
                Client.name.label("nombre"),
                db.func.coalesce(db.func.sum(Sale.total_amount), 0).label("total_compras"),
            )
        ranking_clients = scope_query_to_company(
            ranking_clients_query
            .join(Sale, Client.id == Sale.client_id)
            .filter(_confirmed_sale_status_expression(Sale))
            .group_by(Client.id, Client.name)
            .order_by(db.desc("total_compras")),
            Client,
        ).limit(5).all()
    ranking_categories_query = db.session.query(Product.category.label("category"), db.func.coalesce(db.func.sum(SaleItem.quantity), 0).label("sold"))
    ranking_categories = scope_query_to_company(
        ranking_categories_query
        .join(SaleItem, Product.id == SaleItem.product_id)
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(_confirmed_sale_status_expression(Sale))
        .group_by(Product.category)
        .order_by(db.desc("sold")),
        Product,
    ).limit(8).all()
    recent_sales = (
        _confirmed_sales_query(Sale.query.options(selectinload(Sale.client)), Sale).order_by(Sale.date.desc()).limit(5).all()
        if can_view_economic_metrics
        else []
    )

    cash_stats = {
        "open_sessions": 0,
        "closed_today": 0,
        "sold_cash_today": Decimal("0.00"),
        "sold_mp_today": Decimal("0.00"),
        "sold_total_today": Decimal("0.00"),
        "difference_today": Decimal("0.00"),
        "last_closings": [],
    }
    if can_view_economic_metrics:
        cash_stats["open_sessions"] = scope_query_to_company(CashSession.query.filter(CashSession.status == "abierta"), CashSession).count()
        cash_stats["closed_today"] = scope_query_to_company(
            CashSession.query.filter(CashSession.status == "cerrada", CashSession.closed_at >= today_start),
            CashSession,
        ).count()
        today_sales = _confirmed_sales_query(Sale.query.filter(Sale.date >= today_start), Sale).all()
        for sale in today_sales:
            breakdown = sale_payment_breakdown(sale)
            cash_stats["sold_cash_today"] += breakdown["efectivo"]
            cash_stats["sold_mp_today"] += breakdown["mercado_pago"]
            cash_stats["sold_total_today"] += to_decimal(getattr(sale, "total_amount", 0))

        closed_sessions_today = scope_query_to_company(
            CashSession.query.filter(CashSession.status == "cerrada", CashSession.closed_at >= today_start),
            CashSession,
        ).all()
        for session in closed_sessions_today:
            cash_stats["difference_today"] += to_decimal(getattr(session, "difference_amount", 0))

        cash_stats["last_closings"] = scope_query_to_company(
            CashSession.query.filter(CashSession.status == "cerrada"),
            CashSession,
        ).order_by(CashSession.closed_at.desc()).limit(3).all()

    return {
        "can_view_economic_metrics": can_view_economic_metrics,
        "productos_total": total_products,
        "total_products": total_products,
        "productos_stock": total_products - low_stock_count,
        "productos_bajo_nivel": low_stock_count,
        "low_stock_products": low_stock_count,
        "stock_agotado": out_stock_count,
        "ventas_totales": total_sales_amount,
        "total_sales_amount": total_sales_amount,
        "total_clients": total_clients,
        "clientes_nuevos": new_clients_month,
        "ventas_hoy": sales_today,
        "ventas_semana": sales_week,
        "ventas_mes": sales_month,
        "ingresos_hoy": income_today,
        "ingresos_semana": income_week,
        "ingresos_mes": income_month,
        "ganancia_hoy": profit_today,
        "ganancia_mes": profit_month,
        "rentabilidad": (((profit_month / income_month) * Decimal("100")) if income_month else Decimal("0.00")) if can_view_economic_metrics else None,
        "ticket_promedio": average_ticket,
        "productos_vendidos": sold_units,
        "ventas_recientes": recent_sales,
        "recent_sales": recent_sales,
        "low_stock": scope_query_to_company(Product.query.filter(Product.active.is_(True), Product.stock <= Product.min_stock), Product).order_by(Product.stock.asc()).limit(5).all(),
        "productos_mas_vendidos": top_products,
        "productos_menos_vendidos": least_products,
        "productos_sin_movimiento": [item for item in least_products if not item.sales_count],
        "clientes_recentes": ranking_clients,
        "ranking_clientes": ranking_clients,
        "ranking_categorias": ranking_categories,
        "chart_labels": _last_days_labels(7),
        "chart_sales": _sales_by_day(7) if can_view_economic_metrics else [],
        "chart_categories_labels": [item.category or "Sin categoria" for item in ranking_categories],
        "chart_categories_data": [item.sold or 0 for item in ranking_categories],
        "cash_stats": cash_stats,
    }


def _can_view_economic_metrics():
    from flask_login import current_user

    role = (getattr(current_user, "role", None) or "").strip().lower()
    if role in {"admin", "superadmin"}:
        return True

    raw_permissions = (getattr(current_user, "permissions_json", None) or "").strip()
    if not raw_permissions:
        return False
    try:
        payload = json.loads(raw_permissions)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, list):
        return False
    normalized = {str(item).strip().lower() for item in payload if str(item).strip()}
    return "economic_stats" in normalized


def _sum(column, *filters, model, base_query=None):
    from app import db, scope_query_to_company

    if base_query is not None:
        filtered = base_query
        for condition in filters:
            filtered = filtered.filter(condition)
        subquery = filtered.with_entities(column.label("value")).subquery()
        total = db.session.query(db.func.coalesce(db.func.sum(subquery.c.value), 0)).scalar()
        return to_decimal(total)

    query = scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(column), 0)), model)
    for condition in filters:
        query = query.filter(condition)
    return to_decimal(query.scalar())


def _sum_item_cost(*filters):
    from app import Sale, SaleItem, db, scope_query_to_company

    query = _confirmed_sales_query(
        scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(SaleItem.quantity * SaleItem.cost_price), 0)).join(Sale, SaleItem.sale_id == Sale.id), Sale),
        Sale,
    )
    for condition in filters:
        query = query.filter(condition)
    return to_decimal(query.scalar())


def _sum_item_quantity(*filters):
    from app import Sale, SaleItem, db, scope_query_to_company

    query = _confirmed_sales_query(
        scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(SaleItem.quantity), 0)).join(Sale, SaleItem.sale_id == Sale.id), Sale),
        Sale,
    )
    for condition in filters:
        query = query.filter(condition)
    return query.scalar() or 0


def _confirmed_sale_status_expression(sale_model):
    return or_(sale_model.status.is_(None), func.lower(sale_model.status).in_(list(_confirmed_status_values())))


def _confirmed_status_values():
    return {status.lower() for status in ("confirmada", "confirmed", "aprobada", "approved", "completada", "complete")}


def _confirmed_sales_query(query, sale_model):
    return query.filter(_confirmed_sale_status_expression(sale_model))


def _to_decimal(value):
    """Compatibilidad retroactiva para conversiones locales."""
    return to_decimal(value)


def _last_days_labels(days):
    from app import utcnow

    today = utcnow().date()
    return [(today - timedelta(days=offset)).strftime("%d/%m") for offset in reversed(range(days))]


def _sales_by_day(days):
    from app import Sale, db, scope_query_to_company, utcnow

    today = utcnow().date()
    data = []
    for offset in reversed(range(days)):
        day = today - timedelta(days=offset)
        start = datetime.combine(day, time.min)
        end = datetime.combine(day, time.max)
        total = _confirmed_sales_query(
            scope_query_to_company(
                db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.date >= start, Sale.date <= end),
                Sale,
            ),
            Sale,
        ).scalar() or 0
        data.append(_to_decimal(total))
    return data
