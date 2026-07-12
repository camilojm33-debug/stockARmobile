"""Modulo de gastos con impacto en rentabilidad."""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from app import tenant_required

bp = Blueprint("expenses", __name__)


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


@bp.route("/", methods=["GET", "POST"])
@tenant_required
def index():
    from app import Expense, db, scope_query_to_company

    if request.method == "POST":
        amount = _to_float(request.form.get("amount"))
        description = (request.form.get("description") or "").strip()
        if amount <= 0 or not description:
            flash("Completa descripcion e importe.", "danger")
            return redirect(url_for("expenses.index"))
        db.session.add(
            Expense(
                category=request.form.get("category") or "Otros",
                description=description,
                amount=amount,
                payment_method=request.form.get("payment_method"),
                user_id=current_user.id,
                company_id=getattr(current_user, "company_id", None) or session.get("company_id"),
            )
        )
        db.session.commit()
        flash("Gasto registrado.", "success")
        return redirect(url_for("expenses.index"))

    expenses = scope_query_to_company(Expense.query, Expense).order_by(Expense.date.desc()).limit(50).all()
    total = sum(float(item.amount or 0) for item in expenses)
    return render_template("gastos/index.html", expenses=expenses, total=total)
