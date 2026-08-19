"""Sale workflow orchestrator."""

from decimal import Decimal

from flask import current_app, flash, jsonify, redirect, session, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from stockarmobile.constants import SALE_STATUS_CONFIRMED

from .audit_service import AuditService
from .inventory_service import InventoryService
from .payment_service import PaymentService
from .pricing_service import PricingService
from .totals_service import TotalsService
from .validation_service import ValidationService


class SaleService:
    def __init__(
        self,
        *,
        require_open_cash_session,
        calculate_lines,
        mark_quote_as_converted,
        cart_key,
        to_decimal,
    ):
        self._require_open_cash_session = require_open_cash_session
        self._calculate_lines = calculate_lines
        self._mark_quote_as_converted = mark_quote_as_converted
        self._cart_key = cart_key
        self._to_decimal = to_decimal

    def create_sale_from_items(self, *, items, data, json_response=False):
        from app import CashMovement, Client, Sale, SaleItem, db, record_audit, scope_query_to_company, utcnow

        sale = None
        final_total = Decimal("0.00")
        try:
            current_app.logger.info("[sales] carrito recibido (_create_sale_from_items): items=%s json_response=%s", items, json_response)
            cash_session = self._require_open_cash_session(json_response=json_response)
            if cash_session is None:
                return redirect(url_for("cash.index"))
            if isinstance(cash_session, tuple):
                return cash_session

            checkout_token = ValidationService.sanitize_checkout_token(data.get("checkout_token") or data.get("checkoutToken"))
            company_id = getattr(current_user, "company_id", None)
            if checkout_token:
                existing_sale = scope_query_to_company(Sale.query, Sale).filter(Sale.client_txn_id == checkout_token).first()
                if existing_sale is not None:
                    if json_response:
                        return jsonify({"sale_id": existing_sale.id, "redirect_url": url_for("sales.success", sale_id=existing_sale.id)})
                    return redirect(url_for("sales.success", sale_id=existing_sale.id))

            lines = self._calculate_lines(items, lock_for_update=True, discount_overrides=(data.get("line_discounts") or data.get("line_discount_overrides") or {}))
            sale_totals = PricingService.calculate(lines=[{"price": line["price"], "quantity": line["quantity"], "line_discount": line["discount"]} for line in lines], data=data)
            general_discount = sale_totals["general_discount"]
            surcharge = sale_totals["surcharge"]
            subtotal = sale_totals["subtotal"]
            discount = sale_totals["line_discount_total"]
            tax_total = sale_totals["tax"]
            final_total = sale_totals["total"]

            payment_split = PaymentService.normalize_split(total_amount=final_total, data=data)

            client_id = data.get("client_id") or data.get("cliente_id") or None
            parsed_client_id = None
            if client_id not in (None, ""):
                try:
                    parsed_client_id = int(client_id)
                except (TypeError, ValueError):
                    raise ValueError("Cliente inválido para esta empresa.")

            client = (
                scope_query_to_company(Client.query.filter_by(id=parsed_client_id, active=True), Client).first()
                if parsed_client_id
                else None
            )
            if parsed_client_id and client is None:
                raise ValueError("El cliente seleccionado no pertenece a tu empresa.")

            requiere_comprobante, tipo_comprobante, observacion_comprobante = ValidationService.resolve_comprobante_payload(data)
            if ValidationService.requires_identified_client(data, requiere_comprobante, tipo_comprobante) and client is None:
                raise ValueError("Para ese comprobante debés seleccionar un cliente.")

            sale = Sale(
                customer=client.name if client else "Consumidor final",
                subtotal=subtotal,
                discount=discount + general_discount,
                tax=tax_total,
                total_amount=final_total,
                payment_method=payment_split["primary_method"],
                secondary_payment_method=payment_split["secondary_method"],
                paid_amount=payment_split["primary_amount"],
                secondary_paid_amount=payment_split["secondary_amount"],
                surcharge=surcharge,
                discount_type=sale_totals["discount_adjustment"]["type"],
                discount_value=sale_totals["discount_adjustment"]["value"],
                discount_reason=sale_totals["discount_adjustment"]["reason"],
                surcharge_type=sale_totals["surcharge_adjustment"]["type"],
                surcharge_value=sale_totals["surcharge_adjustment"]["value"],
                surcharge_reason=sale_totals["surcharge_adjustment"]["reason"],
                client_txn_id=checkout_token,
                document_type=data.get("document_type") or data.get("tipo_comprobante") or "venta",
                requiere_comprobante=requiere_comprobante,
                tipo_comprobante=tipo_comprobante,
                observacion_comprobante=observacion_comprobante or None,
                comprobante_emitido=False,
                status=data.get("status") or SALE_STATUS_CONFIRMED,
                qr_reference=data.get("qr_reference"),
                note=data.get("note"),
                client_id=client.id if client else None,
                seller_id=current_user.id,
                company_id=company_id,
                cash_session_id=cash_session.id,
                date=utcnow(),
            )
            payment_breakdown = PaymentService.cash_breakdown(total_amount=final_total, payment_split=payment_split)
            db.session.add(sale)
            db.session.flush()

            cash_amount = self._to_decimal(payment_breakdown.get("efectivo", 0))
            if cash_amount > 0:
                db.session.add(
                    CashMovement(
                        session_id=cash_session.id,
                        user_id=current_user.id,
                        company_id=company_id,
                        sale_id=sale.id,
                        movement_type="ingreso",
                        category="venta",
                        amount=cash_amount,
                        description=f"Venta #{sale.id}",
                    )
                )

            InventoryService.apply_stock(lines=lines)
            for idx, line in enumerate(lines):
                product = line["product"]
                calculated_line = sale_totals["lines"][idx]
                db.session.add(
                    SaleItem(
                        sale_id=sale.id,
                        product_id=product.id,
                        quantity=line["quantity"],
                        price=line["price"],
                        cost_price=self._to_decimal(product.cost_price),
                        discount=calculated_line["final_discount"],
                    )
                )

            self._mark_quote_as_converted(checkout_token, sale.id)

            db.session.commit()
            try:
                AuditService.record_success(record_audit, sale_id=sale.id, final_total=final_total)
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("[sales] no se pudo persistir auditoria post-venta: sale_id=%s", sale.id)
            session.pop(self._cart_key(), None)
        except IntegrityError:
            db.session.rollback()
            token = ValidationService.sanitize_checkout_token(data.get("checkout_token") or data.get("checkoutToken"))
            if token:
                existing_sale = scope_query_to_company(Sale.query, Sale).filter(Sale.client_txn_id == token).first()
                if existing_sale is not None:
                    if json_response:
                        return jsonify({"sale_id": existing_sale.id, "redirect_url": url_for("sales.success", sale_id=existing_sale.id)})
                    return redirect(url_for("sales.success", sale_id=existing_sale.id))
            if json_response:
                return jsonify({"error": "No se pudo completar la venta. Revisa los datos e intenta nuevamente."}), 400
            flash("No se pudo completar la venta por un conflicto de concurrencia.", "danger")
            return redirect(url_for("sales.new_sale"))
        except Exception as exc:
            db.session.rollback()
            try:
                AuditService.record_error(record_audit, error=exc)
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("[sales] no se pudo persistir auditoria de error")
            if json_response:
                message = str(exc)
                safe_message = message if isinstance(exc, ValueError) else "No se pudo completar la venta. Revisa los datos e intenta nuevamente."
                return jsonify({"error": safe_message}), 400
            flash(f"No se pudo completar la venta: {exc}", "danger")
            return redirect(url_for("sales.new_sale"))

        if json_response:
            return jsonify({"sale_id": sale.id, "redirect_url": url_for("sales.success", sale_id=sale.id)})
        flash(f"Venta #{sale.id} realizada con exito. Total: ${final_total:.2f}", "success")
        return redirect(url_for("sales.success", sale_id=sale.id))

    def update_sale(
        self,
        *,
        sale,
        form,
        reason,
        current_open_cash_session,
        sale_snapshot,
        json_dumps,
    ):
        from app import CashMovement, Client, Product, SaleItem, SaleModificationHistory, db, record_audit, scope_query_to_company

        ValidationService.validate_edit_reason(reason)

        existing_line_discount_total = sum(self._to_decimal(item.discount or 0) for item in sale.items)
        sale_order_discount_default = max(self._to_decimal(sale.discount or 0) - existing_line_discount_total, Decimal("0.00"))
        previous_snapshot = sale_snapshot(sale)

        def _resolve_product_for_sale_item(item):
            return item.product or scope_query_to_company(db.session.query(Product), Product).filter(Product.id == item.product_id).first()

        InventoryService.reverse_stock(sale_items=sale.items, resolve_product_func=_resolve_product_for_sale_item)

        product_ids = form.getlist("product_id")
        quantities = form.getlist("quantity")
        prices = form.getlist("price")
        discounts = form.getlist("discount")
        row_deletes = form.getlist("remove_item")
        order_discount = self._to_decimal(form.get("order_discount") or sale_order_discount_default or 0)
        discount_type = form.get("discount_type") or sale.discount_type
        discount_value = form.get("discount_value") or (order_discount if discount_type else None)
        discount_reason = (form.get("discount_reason") or sale.discount_reason or "").strip() or None
        surcharge_type = form.get("surcharge_type") or sale.surcharge_type
        surcharge_value = form.get("surcharge_value") or (sale.surcharge if surcharge_type else None)
        surcharge_reason = (form.get("surcharge_reason") or sale.surcharge_reason or "").strip() or None
        note = (form.get("note") or sale.note or "").strip() or None
        client_id = form.get("client_id") or None
        client = scope_query_to_company(Client.query.filter_by(id=int(client_id), active=True), Client).first() if client_id else None

        product_id_values = []
        for raw_value in product_ids:
            raw_value = (raw_value or "").strip()
            if raw_value:
                product_id_values.append(int(raw_value))

        products_by_id = {}
        if product_id_values:
            products_by_id = {
                product.id: product
                for product in scope_query_to_company(db.session.query(Product), Product).filter(Product.id.in_(product_id_values)).all()
            }

        new_lines = InventoryService.build_edit_lines(
            product_ids=product_ids,
            quantities=quantities,
            prices=prices,
            discounts=discounts,
            row_deletes=row_deletes,
            products_by_id=products_by_id,
            to_decimal=self._to_decimal,
            is_truthy=ValidationService.is_truthy,
        )
        ValidationService.validate_edit_lines(new_lines)

        InventoryService.apply_stock(lines=new_lines)

        sale_totals = TotalsService.calculate(
            [{"price": line["price"], "quantity": line["quantity"], "line_discount": line["discount"]} for line in new_lines],
            general_discount=order_discount,
            surcharge=sale.surcharge or 0,
            discount_type=discount_type,
            discount_value=discount_value,
            discount_reason=discount_reason,
            surcharge_type=surcharge_type,
            surcharge_value=surcharge_value,
            surcharge_reason=surcharge_reason,
        )

        payment_method = form.get("payment_method") or sale.payment_method or "EFECTIVO"
        sale.payment_method = payment_method
        sale.secondary_payment_method = None
        payment_breakdown = PaymentService.cash_breakdown(
            total_amount=sale_totals["total"],
            payment_split={
                "primary_method": payment_method,
                "secondary_method": None,
                "primary_amount": sale_totals["total"],
                "secondary_amount": Decimal("0.00"),
            },
        )

        sale.customer = client.name if client else (sale.customer or "Consumidor final")
        sale.client_id = client.id if client else None
        sale.note = note
        sale.subtotal = sale_totals["subtotal"]
        sale.discount = sale_totals["line_discount_total"] + sale_totals["general_discount"]
        sale.surcharge = sale_totals["surcharge"]
        sale.discount_type = sale_totals["discount_adjustment"]["type"]
        sale.discount_value = sale_totals["discount_adjustment"]["value"]
        sale.discount_reason = sale_totals["discount_adjustment"]["reason"]
        sale.surcharge_type = sale_totals["surcharge_adjustment"]["type"]
        sale.surcharge_value = sale_totals["surcharge_adjustment"]["value"]
        sale.surcharge_reason = sale_totals["surcharge_adjustment"]["reason"]
        sale.tax = sale_totals["tax"]
        sale.total_amount = sale_totals["total"]
        sale.paid_amount = payment_breakdown.get("efectivo", sale_totals["total"])
        sale.secondary_paid_amount = Decimal("0.00")
        sale.status = sale.status or SALE_STATUS_CONFIRMED

        for item in list(sale.items):
            db.session.delete(item)

        for line in new_lines:
            db.session.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=line["product"].id,
                    quantity=float(line["quantity"]),
                    price=line["price"],
                    cost_price=self._to_decimal(line["product"].cost_price),
                    discount=line["discount"],
                )
            )

        cash_movement = CashMovement.query.filter_by(sale_id=sale.id).first()
        cash_amount = self._to_decimal(payment_breakdown.get("efectivo", 0))
        open_cash_session = current_open_cash_session()
        if cash_movement is not None:
            if cash_amount > 0:
                cash_movement.amount = cash_amount
                cash_movement.session_id = sale.cash_session_id or cash_movement.session_id
                cash_movement.user_id = current_user.id
                cash_movement.company_id = sale.company_id
                cash_movement.description = f"Venta #{sale.id} editada"
            else:
                db.session.delete(cash_movement)
        elif cash_amount > 0:
            if open_cash_session is None and not sale.cash_session_id:
                raise ValueError("Debes tener una caja abierta para registrar un cobro en efectivo.")
            db.session.add(
                CashMovement(
                    session_id=sale.cash_session_id or (open_cash_session.id if open_cash_session else None),
                    user_id=current_user.id,
                    company_id=sale.company_id,
                    sale_id=sale.id,
                    movement_type="ingreso",
                    category="venta",
                    amount=cash_amount,
                    description=f"Venta #{sale.id} editada",
                )
            )

        new_snapshot = {
            "sale": {
                "id": sale.id,
                "customer": sale.customer,
                "subtotal": float(sale_totals["subtotal"] or 0),
                "discount": float((sale_totals["line_discount_total"] + sale_totals["general_discount"]) or 0),
                "tax": float(sale_totals["tax"] or 0),
                "total_amount": float(sale_totals["total"] or 0),
                "payment_method": payment_method,
                "secondary_payment_method": None,
                "paid_amount": float(payment_breakdown.get("efectivo", sale_totals["total"]) or 0),
                "secondary_paid_amount": 0.0,
                "note": sale.note,
                "status": sale.status,
            },
            "items": [
                {
                    "product_id": line["product"].id,
                    "product_name": line["product"].name,
                    "unit_measure": line["product"].unit_measure,
                    "quantity": float(line["quantity"]),
                    "price": float(line["price"]),
                    "discount": float(line["discount"]),
                    "total_amount": float((line["price"] * line["quantity"]) - line["discount"]),
                }
                for line in new_lines
            ],
        }
        db.session.add(
            SaleModificationHistory(
                sale_id=sale.id,
                company_id=sale.company_id,
                user_id=current_user.id,
                reason=reason,
                previous_data=json_dumps(previous_snapshot),
                new_data=json_dumps(new_snapshot),
            )
        )

        AuditService.record_update(record_audit, sale_id=sale.id, reason=reason)
        db.session.commit()
        return sale

    def cancel_sale(self, *, sale, detail):
        from app import db, record_audit

        ValidationService.validate_cancel_status(getattr(sale, "status", None))
        sale.status = "anulada"
        AuditService.record_cancel(record_audit, sale_id=sale.id, detail=detail)
        db.session.commit()
        return sale

    def delete_sale(self, *, sale, resolve_product_for_item):
        from app import CashMovement, db, record_audit

        ValidationService.validate_delete_role(getattr(current_user, "role", None))
        CashMovement.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
        InventoryService.reverse_stock(sale_items=sale.items, resolve_product_func=resolve_product_for_item)
        AuditService.record_delete(record_audit, sale_id=sale.id, total_amount=sale.total_amount)
        db.session.delete(sale)
        db.session.commit()
        return sale
