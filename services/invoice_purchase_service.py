"""Explicit, tenant-scoped invoice confirmation transaction."""

from __future__ import annotations

from decimal import Decimal
import json

from sqlalchemy.exc import IntegrityError

from stockarmobile.helpers.dates import utcnow_naive
from services.invoice_matching_service import normalize


class InvoicePurchaseError(ValueError):
    pass


class InvoicePurchaseService:
    @staticmethod
    def confirm(*, company_id: int, user_id: int, invoice: dict, conversation):
        if invoice.get("status") in {"CONFIRMADA", "APLICADA"}:
            return invoice.get("result") or {"status": invoice.get("status")}
        if invoice.get("status") != "LISTA_PARA_CONFIRMAR":
            raise InvoicePurchaseError("La factura todavía no está lista para confirmar.")
        lines = [line for line in (invoice.get("matches") or []) if line.get("matching_status") != "EXCLUIDA"]
        if not lines:
            raise InvoicePurchaseError("La factura no tiene líneas para aplicar.")
        if any(line.get("matching_status") in {"AMBIGUO", "MATCH_PROPUESTO"} for line in lines):
            raise InvoicePurchaseError("Resuelve o excluye las líneas ambiguas antes de confirmar.")
        from app import AuditLog, Product, PurchaseItem, PurchaseOrder, Supplier, db

        supplier_match = invoice.get("supplier_match") or {}
        supplier_status = supplier_match.get("status")
        proposed_supplier_name = str(supplier_match.get("name") or "").strip()
        supplier = None
        create_new_supplier = False
        if supplier_status == "AMBIGUO":
            raise InvoicePurchaseError("Resuelve el proveedor ambiguo antes de confirmar.")
        if supplier_status == "MATCH_EXACTO":
            supplier_id = supplier_match.get("supplier_id")
            supplier = db.session.query(Supplier).filter(Supplier.id == supplier_id, Supplier.company_id == company_id, Supplier.active.is_(True)).first() if supplier_id else None
            if supplier is None:
                raise InvoicePurchaseError("El proveedor seleccionado ya no pertenece a esta empresa.")
        elif supplier_status == "NUEVO_PROVEEDOR_CONFIRMADO":
            if not proposed_supplier_name:
                raise InvoicePurchaseError("No hay nombre de proveedor para crear.")
            create_new_supplier = True
        elif supplier_status == "NUEVO_PROVEEDOR_PROPUESTO" and proposed_supplier_name:
            # Hay un proveedor detectado pero el usuario todavia no decidio: usar existente o crear nuevo (via /resolve).
            raise InvoicePurchaseError("Decide si usar un proveedor existente o crear uno nuevo antes de confirmar.")
        for line in lines:
            if line.get("matching_status") == "NUEVO_PRODUCTO":
                continue
            product_id = line.get("product_id")
            product = db.session.query(Product).filter(Product.id == product_id, Product.company_id == company_id, Product.active.is_(True)).first()
            if product is None:
                raise InvoicePurchaseError("Una coincidencia de producto ya no pertenece a esta empresa.")

        try:
            if create_new_supplier:
                # Doble chequeo tenant-scoped: no confiar en el preview previo, evitar duplicado por nombre normalizado.
                key = normalize(proposed_supplier_name)
                existing_suppliers = db.session.query(Supplier).filter(Supplier.company_id == company_id, Supplier.active.is_(True)).all()
                duplicates = [item for item in existing_suppliers if normalize(item.name) == key]
                if len(duplicates) == 1:
                    raise InvoicePurchaseError("Ya existe un proveedor con ese nombre. Vuelve a resolver la factura para usarlo.")
                if len(duplicates) > 1:
                    raise InvoicePurchaseError("Existen varios proveedores con ese nombre. Resuelve la factura antes de confirmar.")
                supplier = Supplier(name=proposed_supplier_name, company_id=company_id, active=True)
                db.session.add(supplier)
                db.session.flush()
            order = PurchaseOrder(supplier_id=supplier.id if supplier else None, company_id=company_id, date=utcnow_naive(), status="recibida", subtotal=invoice.get("subtotal") or Decimal("0"), total_amount=invoice.get("total") or Decimal("0"), note=f"Carga de factura IA {invoice.get('invoice_number') or ''}".strip(), source_document_hash=invoice.get("document_hash"))
            db.session.add(order)
            try:
                db.session.flush()
            except IntegrityError as exc:
                # La BD es la autoridad final: otra request ya confirmo esta misma factura (company_id + document_hash) concurrentemente.
                db.session.rollback()
                raise InvoicePurchaseError("Esta factura ya fue confirmada por otra solicitud.") from exc
            applied = []
            for index, line in enumerate(lines, start=1):
                quantity = Decimal(str(line["quantity"]))
                unit_cost = Decimal(str(line["unit_cost"]))
                if not quantity.is_finite() or quantity <= 0:
                    raise InvoicePurchaseError("La cantidad de una línea no es válida.")
                if not unit_cost.is_finite() or unit_cost < 0:
                    raise InvoicePurchaseError("El costo de una línea no es válido.")
                product = None
                if line.get("matching_status") == "NUEVO_PRODUCTO":
                    generated_code = f"AI-{str(invoice.get('document_hash') or 'invoice')[:10]}-{index}"
                    product = Product(company_id=company_id, supplier_id=supplier.id if supplier else None, barcode=generated_code, name=line["description"][:200], stock=0, cost_price=0, price=0, active=True)
                    db.session.add(product)
                    db.session.flush()
                else:
                    product = db.session.query(Product).filter(Product.id == line["product_id"], Product.company_id == company_id).with_for_update().first()
                previous_stock = Decimal(str(product.stock or 0))
                previous_cost = Decimal(str(product.cost_price or 0))
                new_stock = previous_stock + quantity
                if not previous_stock.is_finite() or previous_stock < 0 or new_stock < 0:
                    raise InvoicePurchaseError("El stock existente no permite aplicar esta compra de forma segura.")
                average_cost = ((previous_stock * previous_cost) + (quantity * unit_cost)) / new_stock if new_stock else unit_cost
                product.stock = float(new_stock)
                product.cost_price = average_cost
                product.margin = Decimal(str(product.price or 0)) - average_cost
                product.profit_percent = (product.margin / average_cost * 100) if average_cost else 0
                db.session.add(PurchaseItem(purchase_order_id=order.id, product_id=product.id, quantity=float(quantity), unit_cost=unit_cost))
                applied.append({"line_number": line.get("line_number"), "product_id": product.id, "quantity": float(quantity), "unit_cost": str(unit_cost)})
            db.session.add(AuditLog(user_id=user_id, company_id=company_id, action="purchase_create_from_invoice_ai", entity="purchase_order", entity_id=order.id, detail=json.dumps({"document_hash": invoice.get("document_hash"), "invoice_number": invoice.get("invoice_number"), "items": applied}, ensure_ascii=False)))
            result = {"status": "APLICADA", "purchase_order_id": order.id, "items": applied}
            invoice["status"] = "APLICADA"
            invoice["result"] = result
            invoice["confirmed_by_user_id"] = user_id
            invoice["confirmed_at"] = utcnow_naive().isoformat()
            return result
        except Exception:
            db.session.rollback()
            raise