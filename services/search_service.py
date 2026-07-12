"""Busqueda global tipo Spotlight."""


def global_search(term):
    from app import CashSession, Client, Product, PurchaseOrder, Sale, Supplier, scope_query_to_company

    q = (term or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    results = []
    for product in scope_query_to_company(Product.query.filter(Product.active.is_(True), (Product.name.ilike(like)) | (Product.barcode.ilike(like)) | (Product.brand.ilike(like))), Product).limit(8):
        results.append({"type": "Producto", "title": product.name, "subtitle": product.barcode, "url": "/productos/"})
    for client in scope_query_to_company(Client.query.filter(Client.active.is_(True), (Client.name.ilike(like)) | (Client.email.ilike(like)) | (Client.whatsapp.ilike(like))), Client).limit(6):
        results.append({"type": "Cliente", "title": client.name, "subtitle": client.whatsapp or client.email or "", "url": "/clientes/"})
    if q.isdigit():
        sale = scope_query_to_company(Sale.query.filter_by(id=int(q)), Sale).first()
        if sale:
            results.append({"type": "Venta", "title": f"Venta #{sale.id}", "subtitle": sale.customer or "", "url": f"/ventas/{sale.id}"})
    for purchase in scope_query_to_company(PurchaseOrder.query.join(Supplier, PurchaseOrder.supplier_id == Supplier.id, isouter=True), PurchaseOrder).filter(Supplier.name.ilike(like)).limit(5):
        results.append({"type": "Compra", "title": f"Compra #{purchase.id}", "subtitle": purchase.supplier.name if purchase.supplier else "", "url": "/compras/"})
    for supplier in scope_query_to_company(Supplier.query.filter(Supplier.active.is_(True), Supplier.name.ilike(like)), Supplier).limit(5):
        results.append({"type": "Proveedor", "title": supplier.name, "subtitle": supplier.whatsapp or supplier.email or "", "url": "/compras/"})
    for cash in scope_query_to_company(CashSession.query.filter(CashSession.status.ilike(like)), CashSession).limit(3):
        results.append({"type": "Caja", "title": f"Caja #{cash.id}", "subtitle": cash.status, "url": "/caja/"})
    return results[:20]
