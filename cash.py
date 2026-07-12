"""Modulo de caja: apertura, movimientos, arqueo y cierre."""

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from app import tenant_required

bp = Blueprint("cash", __name__)


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


@bp.route("/", methods=["GET", "POST"])
@tenant_required
def index():
    from app import CashMovement, CashSession, db, scope_query_to_company, utcnow

    open_session = scope_query_to_company(CashSession.query.filter_by(user_id=current_user.id, status="abierta"), CashSession).order_by(CashSession.opened_at.desc()).first()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "open" and open_session is None:
            open_session = CashSession(
                user_id=current_user.id,
                company_id=getattr(current_user, "company_id", None) or session.get("company_id"),
                opening_amount=_to_float(request.form.get("opening_amount")),
                note=request.form.get("note"),
            )
            db.session.add(open_session)
            db.session.commit()
            flash("Caja abierta.", "success")
        elif action == "movement" and open_session:
            movement_type = request.form.get("movement_type") or "ingreso"
            amount = _to_float(request.form.get("amount"))
            if amount <= 0:
                flash("El movimiento debe tener importe positivo.", "danger")
            else:
                db.session.add(
                    CashMovement(
                        session_id=open_session.id,
                        user_id=current_user.id,
                        company_id=getattr(current_user, "company_id", None) or session.get("company_id"),
                        movement_type=movement_type,
                        category=request.form.get("category"),
                        amount=amount,
                        description=request.form.get("description"),
                    )
                )
                db.session.commit()
                flash("Movimiento registrado.", "success")
        elif action == "close" and open_session:
            open_session.closed_at = utcnow()
            open_session.closing_amount = _to_float(request.form.get("closing_amount"))
            open_session.status = "cerrada"
            db.session.commit()
            flash("Caja cerrada.", "success")
        return redirect(url_for("cash.index"))

    sessions = scope_query_to_company(CashSession.query, CashSession).order_by(CashSession.opened_at.desc()).limit(20).all()
    movements = scope_query_to_company(CashMovement.query, CashMovement).order_by(CashMovement.created_at.desc()).limit(30).all()
    return render_template("caja/index.html", open_session=open_session, sessions=sessions, movements=movements)
