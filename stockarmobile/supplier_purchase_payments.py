"""Pagos, plazos y vencimientos de compras a proveedores."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_

from stockarmobile.extensions import db


class PurchasePaymentDetail(db.Model):
    __tablename__ = "purchase_payment_details"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    payment_method = db.Column(db.String(40), nullable=False, default="CUENTA_CORRIENTE")
    paid_amount = db.Column(db.Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    secondary_payment_method = db.Column(db.String(40))
    secondary_paid_amount = db.Column(db.Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    payment_term_days = db.Column(db.Integer, nullable=False, default=0)
    due_date = db.Column(db.Date, nullable=True, index=True)
    payment_status = db.Column(db.String(20), nullable=False, default="pendiente", index=True)
    payment_reference = db.Column(db.String(120))
    notes = db.Column(db.Text)
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    purchase_order = db.relationship("PurchaseOrder", backref=db.backref("payment_detail", uselist=False))


PAYMENT_METHODS = [
    ("EFECTIVO", "Efectivo"),
    ("TRANSFERENCIA", "Transferencia bancaria"),
    ("TARJETA", "Tarjeta"),
    ("MERCADOPAGO", "Mercado Pago"),
    ("CHEQUE", "Cheque"),
    ("CUENTA_CORRIENTE", "Cuenta corriente"),
    ("OTROS", "Otro"),
]


def _company_id():
    if getattr(current_user, "role", None) == "superadmin":
        return request.values.get("company_id", type=int)
    return getattr(current_user, "company_id", None)


def _guard():
    if getattr(current_user, "role", None) not in {"admin", "superadmin"}:
        abort(403)


def _purchase_context(supplier_id, purchase_id, company_id):
    from app import PurchaseOrder, Supplier

    supplier = Supplier.query.filter_by(id=supplier_id, company_id=company_id).first_or_404()
    purchase = PurchaseOrder.query.filter_by(id=purchase_id, supplier_id=supplier.id, company_id=company_id).first_or_404()
    return supplier, purchase


def _parse_money(value):
    try:
        return max(Decimal("0"), Decimal(str(value or "0").replace(",", ".")))
    except Exception:
        return Decimal("0")


def _parse_due_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def register_routes(bp):
    @bp.route("/proveedores/<int:supplier_id>/compras/<int:purchase_id>/pago", methods=["GET", "POST"])
    @login_required
    def supplier_purchase_payment(supplier_id, purchase_id):
        _guard()
        company_id = _company_id()
        if not company_id:
            abort(403)
        supplier, purchase = _purchase_context(supplier_id, purchase_id, company_id)
        detail = PurchasePaymentDetail.query.filter_by(purchase_order_id=purchase.id, company_id=company_id).first()

        if detail is None:
            detail = PurchasePaymentDetail(
                purchase_order_id=purchase.id,
                company_id=company_id,
                payment_method="CUENTA_CORRIENTE",
                paid_amount=Decimal("0.00"),
                secondary_paid_amount=Decimal("0.00"),
                payment_term_days=0,
                payment_status="pendiente",
            )

        if request.method == "POST":
            primary = (request.form.get("payment_method") or "CUENTA_CORRIENTE").strip().upper()
            secondary = (request.form.get("secondary_payment_method") or "").strip().upper() or None
            paid = _parse_money(request.form.get("paid_amount"))
            secondary_paid = _parse_money(request.form.get("secondary_paid_amount")) if secondary else Decimal("0")
            total = Decimal(str(purchase.total_amount or 0))
            if primary not in {key for key, _ in PAYMENT_METHODS}:
                primary = "OTROS"
            if secondary and secondary not in {key for key, _ in PAYMENT_METHODS}:
                secondary = None
                secondary_paid = Decimal("0")
            if secondary and secondary == primary:
                flash("El segundo medio de pago debe ser diferente al principal.", "warning")
                return render_template("compras/supplier_purchase_payment.html", supplier=supplier, purchase=purchase, detail=detail, payment_methods=PAYMENT_METHODS, selected_company_id=company_id)
            if paid + secondary_paid > total:
                flash("Los importes pagados no pueden superar el total de la compra.", "danger")
                return render_template("compras/supplier_purchase_payment.html", supplier=supplier, purchase=purchase, detail=detail, payment_methods=PAYMENT_METHODS, selected_company_id=company_id)

            term_days = max(0, request.form.get("payment_term_days", type=int) or 0)
            due_date = _parse_due_date(request.form.get("due_date"))
            if term_days and due_date is None:
                due_date = (purchase.date.date() if purchase.date else date.today()) + timedelta(days=term_days)
            if due_date and not term_days:
                base_date = purchase.date.date() if purchase.date else date.today()
                term_days = max(0, (due_date - base_date).days)

            status = (request.form.get("payment_status") or "").strip().lower()
            calculated_status = "pagada" if paid + secondary_paid >= total and total > 0 else ("parcial" if paid + secondary_paid > 0 else "pendiente")
            if status not in {"pendiente", "parcial", "pagada"}:
                status = calculated_status
            elif status == "pagada" and paid + secondary_paid < total:
                status = calculated_status

            detail.payment_method = primary
            detail.paid_amount = paid
            detail.secondary_payment_method = secondary
            detail.secondary_paid_amount = secondary_paid
            detail.payment_term_days = term_days
            detail.due_date = due_date
            detail.payment_status = status
            detail.payment_reference = (request.form.get("payment_reference") or "").strip() or None
            detail.notes = (request.form.get("payment_notes") or "").strip() or None
            detail.paid_at = datetime.utcnow() if status == "pagada" else None
            db.session.add(detail)
            db.session.commit()
            flash("Información de pago guardada.", "success")
            return redirect(url_for("purchases.supplier_purchases", supplier_id=supplier.id, company_id=company_id))

        return render_template(
            "compras/supplier_purchase_payment.html",
            supplier=supplier,
            purchase=purchase,
            detail=detail,
            payment_methods=PAYMENT_METHODS,
            selected_company_id=company_id,
        )

    return bp


def register_notification_extension():
    """Extiende el centro existente sin reemplazar su lógica actual."""
    try:
        from services import notification_service
    except Exception:
        return

    if getattr(notification_service, "_supplier_payment_extension", False):
        return

    original_build = notification_service.build_notifications

    def build_notifications_with_supplier_payments():
        items = original_build()
        if not getattr(current_user, "is_authenticated", False) or getattr(current_user, "role", None) != "admin":
            return items
        company_id = getattr(current_user, "company_id", None)
        if not company_id:
            return items
        try:
            from app import PurchaseOrder, Supplier, utcnow
            now = utcnow()
            horizon = now.date() + timedelta(days=7)
            rows = (
                db.session.query(PurchasePaymentDetail, PurchaseOrder, Supplier)
                .join(PurchaseOrder, PurchaseOrder.id == PurchasePaymentDetail.purchase_order_id)
                .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
                .filter(
                    PurchasePaymentDetail.company_id == company_id,
                    PurchaseOrder.company_id == company_id,
                    Supplier.company_id == company_id,
                    PurchasePaymentDetail.payment_status.in_(["pendiente", "parcial"]),
                    PurchasePaymentDetail.due_date.isnot(None),
                    PurchasePaymentDetail.due_date <= horizon,
                )
                .order_by(PurchasePaymentDetail.due_date.asc(), PurchaseOrder.id.asc())
                .limit(8)
                .all()
            )
            for detail, purchase, supplier in rows:
                remaining = max(Decimal("0"), Decimal(str(purchase.total_amount or 0)) - Decimal(str(detail.paid_amount or 0)) - Decimal(str(detail.secondary_paid_amount or 0)))
                days = (detail.due_date - now.date()).days
                if days < 0:
                    label = f"Vencido hace {abs(days)} día(s)"
                    kind = "danger"
                elif days == 0:
                    label = "Vence hoy"
                    kind = "danger"
                else:
                    label = f"Vence en {days} día(s)"
                    kind = "warning"
                items.append({
                    "type": kind,
                    "title": "Pago a proveedor",
                    "body": f"{supplier.name} · compra #{purchase.id} · {label} · pendiente ${float(remaining):.2f}.",
                    "href": url_for("purchases.supplier_purchase_payment", supplier_id=supplier.id, purchase_id=purchase.id, company_id=company_id),
                })
        except Exception:
            return items
        return items

    notification_service.build_notifications = build_notifications_with_supplier_payments
    notification_service._supplier_payment_extension = True


# The models package imports this module during app creation, before purchases is registered.
from purchases import bp as purchases_bp  # noqa: E402
register_routes(purchases_bp)
register_notification_extension()
