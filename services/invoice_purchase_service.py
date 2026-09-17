"""Explicit, tenant-scoped invoice confirmation transaction."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re

from sqlalchemy.exc import IntegrityError

from stockarmobile.helpers.dates import utcnow_naive
from services.invoice_matching_service import normalize


class InvoicePurchaseError(ValueError):
    pass


def _invoice_location_from_path(path: str):
    match = re.search(r"/dashboard/ai-agent/invoices/([^/]+)/(?:resolve|resolve-code|confirm)$", path or "")
    return match.group(1) if match else None


def _find_invoice_upload(upload_id: str, company_id: int):
    from stockarmobile.models.conversations import Conversation

    for conversation in Conversation.query.filter_by(company_id=company_id).all():
        metadata = conversation.metadata_json or {}
        for upload in metadata.get("invoice_uploads") or []:
            if str(upload.get("upload_id")) == str(upload_id):
                return conversation, upload
    return None


def _register_invoice_mutation_guard() -> None:
    """Never allow product/provider edits after the invoice was applied."""
    try:
        from flask import jsonify, request
        from flask_login import current_user
        from app import app
    except Exception:
        return
    if getattr(app, "_invoice_mutation_guard_registered", False):
        return
    app._invoice_mutation_guard_registered = True

    @app.before_request
    def _invoice_mutation_guard():
        if request.method != "POST" or request.path.endswith("/confirm"):
            return None
        upload_id = _invoice_location_from_path(request.path or "")
        if upload_id is None:
            return None
        try:
            if not current_user.is_authenticated:
                return None
            company_id = getattr(current_user, "company_id", None)
            if not company_id:
                return None
            found = _find_invoice_upload(upload_id, int(company_id))
            if found is not None:
                _, upload = found
                if str(upload.get("status") or "").upper() in {"APLICADA", "CONFIRMADA"}:
                    return jsonify({
                        "success": False,
                        "error": "La factura ya fue aplicada. No se pueden modificar sus productos o proveedor.",
                    }), 409
        except Exception:
            return None
        return None


_register_invoice_mutation_guard()


class InvoicePurchaseService:
    @staticmethod
    def _decimal(value, field: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise InvoicePurchaseError(f"{field} no es válido.") from exc
        if not result.is_finite() or result < minimum:
            raise InvoicePurchaseError(f"{field} no es válido.")
        return result

    @staticmethod
    def _prepare_invoice(invoice: dict):
        currency = str(invoice.get("currency") or "").upper().strip()
        if currency != "ARS":
            raise InvoicePurchaseError(
                "La factura no se puede aplicar todavía: la carga de compras IA está habilitada actualmente solo para ARS."
            )
        subtotal = InvoicePurchaseService._decimal(invoice.get("subtotal"), "El subtotal de la factura")
        tax = InvoicePurchaseService._decimal(invoice.get("tax"), "El IVA de la factura")
        total = InvoicePurchaseService._decimal(invoice.get("total"), "El total de la factura")
        if abs((subtotal + tax) - total) > Decimal("0.05"):
            raise InvoicePurchaseError("El subtotal + IVA no coincide con el total de la factura.")
        return currency, subtotal, tax, total

    @staticmethod
    def confirm(*, company_id: int, user_id: int, invoice: dict, conversation):
        if invoice.get("status") in {"CONFIRMADA", "APLICADA"}:
            return invoice.get("result") or {"status": invoice.get("status")}
        if invoice.get("status") != "LISTA_PARA_CONFIRMAR":
            raise InvoicePurchaseError("La factura todavía no está lista para confirmar.")

        lines = [line for line in (invoice.get("matches") or []) if line.get("matching_status") != "EXCLUIDA"]
        if not lines:
            raise InvoicePurchaseError("La factura no tiene líneas para aplicar.")
        if any(line.get("matching_status") in {"AMBIGUO", "MATCH_PROPUESTO", "NUEVO_PRODUCTO"} for line in lines):
            raise InvoicePurchaseError("Resuelve todos los productos antes de confirmar la factura.")
        if any(not line.get("product_id") for line in lines):
            raise InvoicePurchaseError("Cada línea debe tener un producto StockAR asignado antes de confirmar.")
        if any(line.get("quantity") in (None, "") or line.get("unit_cost") in (None, "") for line in lines):
            raise InvoicePurchaseError("Hay líneas sin cantidad o costo unitario.")
        currency, subtotal, tax, total = InvoicePurchaseService._prepare_invoice(invoice)

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
            supplier = (
                db.session.query(Supplier)
                .filter(Supplier.id == supplier_id, Supplier.company_id == company_id, Supplier.active.is_(True))
                .first()
                if supplier_id
                else None
            )
            if supplier is None:
                raise InvoicePurchaseError("El proveedor seleccionado ya no pertenece a esta empresa.")
        elif supplier_status == "NUEVO_PROVEEDOR_CONFIRMADO":
            if not proposed_supplier_name:
                raise InvoicePurchaseError("No hay nombre de proveedor para crear.")
            create_new_supplier = True
        elif supplier_status == "NUEVO_PROVEEDOR_PROPUESTO" and proposed_supplier_name:
            raise InvoicePurchaseError("Decide si usar un proveedor existente o crear uno nuevo antes de confirmar.")
        else:
            raise InvoicePurchaseError("El proveedor debe resolverse antes de confirmar.")

        for line in lines:
            product_id = line.get("product_id")
            product = (
                db.session.query(Product)
                .filter(Product.id == product_id, Product.company_id == company_id, Product.active.is_(True))
                .first()
            )
            if product is None:
                raise InvoicePurchaseError("Una coincidencia de producto ya no pertenece a esta empresa.")

        try:
            if create_new_supplier:
                key = normalize(proposed_supplier_name)
                existing_suppliers = (
                    db.session.query(Supplier)
                    .filter(Supplier.company_id == company_id, Supplier.active.is_(True))
                    .all()
                )
                duplicates = [item for item in existing_suppliers if normalize(item.name) == key]
                if len(duplicates) == 1:
                    raise InvoicePurchaseError("Ya existe un proveedor con ese nombre. Volvé a resolver la factura para usarlo.")
                if len(duplicates) > 1:
                    raise InvoicePurchaseError("Existen varios proveedores con ese nombre. Resolvé la factura antes de confirmar.")
                supplier = Supplier(name=proposed_supplier_name, company_id=company_id, active=True)
                db.session.add(supplier)
                db.session.flush()

            order = PurchaseOrder(
                supplier_id=supplier.id if supplier else None,
                company_id=company_id,
                date=utcnow_naive(),
                status="recibida",
                subtotal=subtotal,
                total_amount=total,
                note=f"Carga de factura IA {invoice.get('invoice_number') or ''} · Moneda {currency}".strip(),
                source_document_hash=invoice.get("document_hash"),
            )
            db.session.add(order)
            try:
                db.session.flush()
            except IntegrityError as exc:
                db.session.rollback()
                raise InvoicePurchaseError("Esta factura ya fue confirmada por otra solicitud.") from exc

            applied = []
            result_items = []
            match_by_line = {str(item.get("line_number")): item for item in (invoice.get("matches") or [])}

            for index, line in enumerate(lines, start=1):
                quantity = InvoicePurchaseService._decimal(
                    line.get("quantity"), f"La cantidad de la línea {index}", minimum=Decimal("0.000001")
                )
                unit_cost = InvoicePurchaseService._decimal(line.get("unit_cost"), f"El costo de la línea {index}")
                product = (
                    db.session.query(Product)
                    .filter(Product.id == line.get("product_id"), Product.company_id == company_id, Product.active.is_(True))
                    .with_for_update()
                    .first()
                )
                if product is None:
                    raise InvoicePurchaseError("No se pudo bloquear el producto para aplicar la compra.")

                previous_stock = Decimal(str(product.stock or 0))
                previous_cost = Decimal(str(product.cost_price or 0))
                if not previous_stock.is_finite() or previous_stock < 0:
                    raise InvoicePurchaseError("El stock existente no permite aplicar esta compra de forma segura.")
                new_stock = previous_stock + quantity
                average_cost = ((previous_stock * previous_cost) + (quantity * unit_cost)) / new_stock if new_stock else unit_cost

                product.stock = float(new_stock)
                product.cost_price = average_cost
                product.supplier_id = supplier.id if supplier else product.supplier_id
                product.margin = Decimal(str(product.price or 0)) - average_cost
                product.profit_percent = (product.margin / average_cost * 100) if average_cost else 0
                db.session.add(product)

                line_number = line.get("line_number")
                persisted_line = match_by_line.get(str(line_number))
                if persisted_line is not None:
                    persisted_line.update({
                        "matching_status": "APLICADO",
                        "line_state": "APLICADO",
                        "product_id": product.id,
                        "product_name": product.name,
                        "product_code": product.barcode,
                        "final_unit_cost": str(unit_cost),
                        "sale_price": str(product.price or 0),
                        "supplier_id": supplier.id if supplier else None,
                    })

                purchase_item = PurchaseItem(
                    purchase_order_id=order.id,
                    product_id=product.id,
                    quantity=float(quantity),
                    unit_cost=unit_cost,
                )
                db.session.add(purchase_item)
                row = {
                    "line_number": line_number,
                    "product_id": product.id,
                    "product_code": product.barcode,
                    "quantity": float(quantity),
                    "unit_cost": str(unit_cost),
                    "sale_price": str(product.price or 0),
                }
                applied.append(row)
                result_items.append(row)

            invoice["matches"] = list(match_by_line.values())
            result = {
                "status": "APLICADA",
                "purchase_order_id": order.id,
                "currency": currency,
                "items": result_items,
            }
            invoice["status"] = "APLICADA"
            invoice["result"] = result
            invoice["confirmed_by_user_id"] = user_id
            invoice["confirmed_at"] = utcnow_naive().isoformat()

            db.session.add(AuditLog(
                user_id=user_id,
                company_id=company_id,
                action="purchase_create_from_invoice_ai",
                entity="purchase_order",
                entity_id=order.id,
                detail=json.dumps({
                    "document_hash": invoice.get("document_hash"),
                    "invoice_number": invoice.get("invoice_number"),
                    "currency": currency,
                    "items": applied,
                }, ensure_ascii=False),
            ))
            db.session.commit()
            return result
        except InvoicePurchaseError:
            db.session.rollback()
            raise
        except Exception:
            db.session.rollback()
            raise
