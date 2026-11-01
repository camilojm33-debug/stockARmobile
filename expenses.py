"""Modulo de gastos con impacto en rentabilidad."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app import tenant_required
from stockarmobile.helpers.numbers import safe_float

bp = Blueprint("expenses", __name__)


def _to_float(value, default=0.0):
    return safe_float(value, default)


def _record_cash_expense_movement(*, user, expense):
    """Mirror an EFECTIVO expense in the operator's open drawer atomically."""
    payment_method = str(getattr(expense, "payment_method", "") or "").strip().lower()
    if payment_method not in {"efectivo", "cash", "contado"}:
        return None

    from app import CashMovement, CashSession, db

    company_id = getattr(user, "company_id", None)
    from stockarmobile.branch_context import get_active_branch_id
    active_branch_id = get_active_branch_id()
    open_session_query = CashSession.query.filter_by(
        company_id=company_id,
        user_id=user.id,
        status="abierta",
    )
    if active_branch_id is not None:
        open_session_query = open_session_query.filter(CashSession.branch_id == active_branch_id)
    open_session = (
        open_session_query.order_by(CashSession.opened_at.desc(), CashSession.id.desc())
        .with_for_update()
        .first()
    )
    if open_session is None:
        return None

    movement = CashMovement(
        session_id=open_session.id,
        user_id=user.id,
        company_id=company_id,
        branch_id=getattr(open_session, "branch_id", None),
        movement_type="egreso",
        category="gasto",
        amount=expense.amount,
        description=f"Gasto #{expense.id}: {expense.description}",
    )
    db.session.add(movement)
    db.session.flush()
    return movement


@bp.route("/", methods=["GET", "POST"])
@tenant_required
def index():
    from app import Expense, db, record_audit, scope_query_to_company

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
            branch_id=__import__("stockarmobile.branch_context", fromlist=["get_active_branch_id"]).get_active_branch_id(),
        )
        db.session.add(expense)
        db.session.flush()

        # Both records share this transaction, so rollback cannot leave only one.
        _record_cash_expense_movement(user=current_user, expense=expense)

        record_audit(action="expense_create", entity="expense", entity_id=expense.id, detail=f"Gasto {expense.category} por {amount}")
        db.session.commit()
        flash("Gasto registrado.", "success")
        return redirect(url_for("expenses.index"))

    expense_query = scope_query_to_company(Expense.query, Expense)
    from stockarmobile.branch_context import get_active_branch_id
    branch_id = get_active_branch_id()
    if branch_id is not None:
        expense_query = expense_query.filter(Expense.branch_id == branch_id)
    expenses = expense_query.order_by(Expense.date.desc()).limit(50).all()
    total = sum(float(item.amount or 0) for item in expenses)
    return render_template("gastos/index.html", expenses=expenses, total=total)
