"""Modulo de gastos con impacto en rentabilidad."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app import tenant_required
from stockarmobile.helpers.numbers import safe_float

bp = Blueprint("expenses", __name__)


def _to_float(value, default=0.0):
    return safe_float(value, default)


@bp.route("/", methods=["GET", "POST"])
@tenant_required
def index():
    from app import CashMovement, CashSession, Expense, db, record_audit, scope_query_to_company

    if request.method == "POST":
        amount = _to_float(request.form.get("amount"))
        description = (request.form.get("description") or "").strip()
        if amount <= 0 or not description:
            flash("Completa descripcion e importe.", "danger")
            return redirect(url_for("expenses.index"))
        expense = Expense(
            category=request.form.get("category") or "Otros",
            description=description,
            amount=amount,
            payment_method=request.form.get("payment_method"),
            user_id=current_user.id,
            company_id=getattr(current_user, "company_id", None),
        )
        db.session.add(expense)
        db.session.flush()

        payment_method = str(expense.payment_method or "").strip().lower()
        if payment_method in {"efectivo", "cash", "contado"}:
            # A cash expense must also be reflected in the current operator's
            # open drawer. Keep both records in one transaction so a rollback
            # cannot leave the expense and cash balance out of sync.
            company_id = getattr(current_user, "company_id", None)
            open_session = (
                CashSession.query
                .filter_by(
                    company_id=company_id,
                    user_id=current_user.id,
                    status="abierta",
                )
                .order_by(CashSession.opened_at.desc(), CashSession.id.desc())
                .with_for_update()
                .first()
            )
            if open_session is not None:
                db.session.add(CashMovement(
                    session_id=open_session.id,
                    user_id=current_user.id,
                    company_id=company_id,
                    movement_type="egreso",
                    category="gasto",
                    amount=amount,
                    description=f"Gasto #{expense.id}: {expense.description}",
                ))
                db.session.flush()

        record_audit(action="expense_create", entity="expense", entity_id=expense.id, detail=f"Gasto {expense.category} por {amount}")
        db.session.commit()
        flash("Gasto registrado.", "success")
        return redirect(url_for("expenses.index"))

    expenses = scope_query_to_company(Expense.query, Expense).order_by(Expense.date.desc()).limit(50).all()
    total = sum(float(item.amount or 0) for item in expenses)
    return render_template("gastos/index.html", expenses=expenses, total=total)
