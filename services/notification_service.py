"""Centro de notificaciones operativo."""


def build_notifications():
    from app import CashSession, Product, PurchaseOrder, Sale, db, scope_query_to_company, utcnow
    from datetime import datetime, time

    today_start = datetime.combine(utcnow().date(), time.min)
    low_stock = scope_query_to_company(Product.query.filter(Product.active.is_(True), Product.stock <= Product.min_stock, Product.stock > 0), Product).count()
    out_stock = scope_query_to_company(Product.query.filter(Product.active.is_(True), Product.stock <= 0), Product).count()
    open_cash = scope_query_to_company(CashSession.query.filter_by(status="abierta"), CashSession).count()
    pending_purchases = scope_query_to_company(PurchaseOrder.query.filter(PurchaseOrder.status.in_(["pendiente", "ordenada"])), PurchaseOrder).count()
    sales_today = scope_query_to_company(Sale.query.filter(Sale.date >= today_start), Sale).count()
    sales_amount = scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.date >= today_start), Sale).scalar() or 0

    items = []
    if low_stock:
        items.append({"type": "warning", "title": "Stock critico", "body": f"{low_stock} productos necesitan reposicion.", "href": "/productos/?low_stock=1"})
    if out_stock:
        items.append({"type": "danger", "title": "Productos agotados", "body": f"{out_stock} productos sin stock.", "href": "/productos/"})
    if open_cash:
        items.append({"type": "info", "title": "Caja abierta", "body": f"{open_cash} caja(s) pendientes de cierre.", "href": "/caja/"})
    if pending_purchases:
        items.append({"type": "primary", "title": "Compras pendientes", "body": f"{pending_purchases} orden(es) requieren seguimiento.", "href": "/compras/"})
    items.append({"type": "success", "title": "Ventas del dia", "body": f"{sales_today} ventas · ${float(sales_amount):.2f}", "href": "/ventas/"})
    return items
