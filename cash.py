"""Modulo de caja: apertura, movimientos, arqueo, cierre y administracion."""

import csv
from decimal import Decimal
from io import StringIO

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user

from app import tenant_required

bp = Blueprint("cash", __name__)


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _to_decimal(value, default="0.00"):
    try:
        if value in (None, ""):
            return Decimal(default)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (TypeError, ValueError):
        return Decimal(default)


def _format_decimal(value):
    return _to_decimal(value)


def _is_admin():
    return getattr(current_user, "role", None) == "admin"


def _base_session_query():
    from app import CashSession, scope_query_to_company

    query = scope_query_to_company(CashSession.query, CashSession)
    if not _is_admin():
        query = query.filter(CashSession.user_id == current_user.id)
    return query


def _base_movement_query():
    from app import CashMovement, CashSession, scope_query_to_company

    query = scope_query_to_company(CashMovement.query, CashMovement)
    if not _is_admin():
        query = query.join(CashSession, CashSession.id == CashMovement.session_id).filter(CashSession.user_id == current_user.id)
    return query


def _current_open_session():
    from app import CashSession

    return _base_session_query().filter(CashSession.status == "abierta", CashSession.user_id == current_user.id).order_by(CashSession.opened_at.desc()).first()


def _session_summary(session):
    opening = _to_decimal(session.opening_amount)
    income = Decimal("0.00")
    expense = Decimal("0.00")
    for movement in session.movements:
        movement_amount = _to_decimal(movement.amount)
        movement_type = (movement.movement_type or "").strip().lower()
        if movement_type in {"egreso", "retiro", "salida", "gasto"}:
            expense += movement_amount
        else:
            income += movement_amount
    expected = opening + income - expense
    counted = _to_decimal(session.counted_amount) if session.counted_amount is not None else None
    difference = _to_decimal(session.difference_amount) if session.difference_amount is not None else (counted - expected if counted is not None else None)
    return {
        "opening": opening,
        "income": income,
        "expense": expense,
        "expected": expected,
        "counted": counted,
        "difference": difference,
    }


def _get_session_or_404(session_id):
    from app import CashSession, scope_query_to_company

    session = scope_query_to_company(CashSession.query, CashSession).filter(CashSession.id == session_id).first_or_404()
    if not _is_admin() and session.user_id != current_user.id:
        abort(403)
    return session


def _session_ticket_text(session):
    summary = _session_summary(session)
    lines = [
        "STOCK ARMOBILE - CAJA",
        "-" * 32,
        f"Caja: #{session.id}",
        f"Usuario: {session.user.name if session.user else session.user_id}",
        f"Estado: {session.status}",
        f"Apertura: {session.opened_at:%Y-%m-%d %H:%M}",
    ]
    if session.closed_at:
        lines.append(f"Cierre: {session.closed_at:%Y-%m-%d %H:%M}")
    lines.extend([
        f"Apertura inicial: ${summary['opening']:.2f}",
        f"Ingresos: ${summary['income']:.2f}",
        f"Egresos: ${summary['expense']:.2f}",
        f"Esperado: ${summary['expected']:.2f}",
    ])
    if summary["counted"] is not None:
        lines.append(f"Contado: ${summary['counted']:.2f}")
    if summary["difference"] is not None:
        lines.append(f"Diferencia: ${summary['difference']:.2f}")
    lines.append("-" * 32)
    for movement in session.movements:
        lines.append(f"{movement.created_at:%Y-%m-%d %H:%M} | {movement.movement_type} | {movement.category or '-'} | ${movement.amount:.2f}")
    return "\n".join(lines)


@bp.route("/", methods=["GET", "POST"])
@tenant_required
def index():
    from app import CashMovement, CashSession, Company, User, db, record_audit, scope_query_to_company, utcnow

    status_filter = (request.args.get("status") or "").strip().lower()
    user_filter = request.args.get("user_id", type=int)
    search = (request.args.get("q") or "").strip().lower()

    sessions_query = _base_session_query()
    if status_filter in {"abierta", "cerrada", "anulada"}:
        sessions_query = sessions_query.filter(CashSession.status == status_filter)
    if user_filter and _is_admin():
        sessions_query = sessions_query.filter(CashSession.user_id == user_filter)
    if search:
        like = f"%{search}%"
        sessions_query = sessions_query.filter((CashSession.note.ilike(like)) | (CashSession.closing_note.ilike(like)) | (CashSession.void_reason.ilike(like)))

    open_session = _current_open_session()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        session_id = request.form.get("session_id", type=int)
        target_session = _get_session_or_404(session_id) if session_id else open_session

        if action == "open":
            if open_session is not None:
                flash("Ya tienes una caja abierta.", "warning")
            else:
                target_session = CashSession(
                    user_id=current_user.id,
                    company_id=getattr(current_user, "company_id", None),
                    opened_by_user_id=current_user.id,
                    opening_amount=_to_decimal(request.form.get("opening_amount")),
                    note=request.form.get("note"),
                    status="abierta",
                )
                db.session.add(target_session)
                db.session.flush()
                record_audit(action="cash_open", entity="cash_session", entity_id=target_session.id, detail="Apertura de caja")
                db.session.commit()
                flash("Caja abierta.", "success")
        elif action == "movement":
            if target_session is None or target_session.status != "abierta":
                flash("Debes tener una caja abierta para registrar movimientos.", "warning")
            else:
                amount = _to_decimal(request.form.get("amount"))
                if amount <= 0:
                    flash("El movimiento debe tener un importe positivo.", "danger")
                else:
                    movement_type = (request.form.get("movement_type") or "ingreso").strip().lower()
                    db.session.add(
                        CashMovement(
                            session_id=target_session.id,
                            user_id=current_user.id,
                            company_id=getattr(current_user, "company_id", None),
                            movement_type=movement_type,
                            category=request.form.get("category"),
                            amount=amount,
                            description=request.form.get("description"),
                        )
                    )
                    db.session.flush()
                    record_audit(action="cash_movement", entity="cash_movement", detail=f"Movimiento {movement_type} por {amount}")
                    db.session.commit()
                    flash("Movimiento registrado.", "success")
        elif action == "close":
            if target_session is None or target_session.status != "abierta":
                flash("No hay una caja abierta para cerrar.", "warning")
            else:
                summary = _session_summary(target_session)
                counted_amount = _to_decimal(request.form.get("counted_amount") or request.form.get("closing_amount") or summary["expected"])
                target_session.closed_at = utcnow()
                target_session.expected_amount = summary["expected"]
                target_session.counted_amount = counted_amount
                target_session.closing_amount = counted_amount
                target_session.difference_amount = counted_amount - summary["expected"]
                target_session.closing_note = request.form.get("closing_note")
                target_session.status = "cerrada"
                record_audit(action="cash_close", entity="cash_session", entity_id=target_session.id, detail=f"Cierre de caja diferencia={target_session.difference_amount}")
                db.session.commit()
                flash("Caja cerrada.", "success")
        elif action == "reopen":
            if not _is_admin():
                abort(403)
            if target_session is None:
                flash("Selecciona una caja para reabrir.", "warning")
            else:
                target_session.status = "abierta"
                target_session.closed_at = None
                target_session.closing_amount = None
                target_session.counted_amount = None
                target_session.difference_amount = None
                target_session.closing_note = None
                target_session.reopened_at = utcnow()
                target_session.reopened_by_user_id = current_user.id
                target_session.voided_at = None
                target_session.voided_by_user_id = None
                target_session.void_reason = None
                record_audit(action="cash_reopen", entity="cash_session", entity_id=target_session.id, detail="Reapertura de caja")
                db.session.commit()
                flash("Caja reabierta.", "success")
        elif action == "void":
            if not _is_admin():
                abort(403)
            if target_session is None:
                flash("Selecciona una caja para anular.", "warning")
            else:
                target_session.status = "anulada"
                target_session.voided_at = utcnow()
                target_session.voided_by_user_id = current_user.id
                target_session.void_reason = request.form.get("void_reason")
                record_audit(action="cash_void", entity="cash_session", entity_id=target_session.id, detail="Caja anulada")
                db.session.commit()
                flash("Caja anulada.", "success")
        else:
            flash("Acción no reconocida.", "warning")

        return redirect(url_for("cash.index"))

    sessions = sessions_query.order_by(CashSession.opened_at.desc()).limit(25).all()
    movements_query = _base_movement_query()
    if status_filter in {"abierta", "cerrada", "anulada"}:
        movements_query = movements_query.join(CashMovement.session).filter(CashSession.status == status_filter)
    if user_filter and _is_admin():
        movements_query = movements_query.join(CashMovement.session).filter(CashSession.user_id == user_filter)
    if search:
        like = f"%{search}%"
        movements_query = movements_query.filter((CashMovement.category.ilike(like)) | (CashMovement.description.ilike(like)))
    movements = movements_query.order_by(CashMovement.created_at.desc()).limit(40).all()

    session_summary = _session_summary(open_session) if open_session else None
    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    employees = []
    if _is_admin():
        employees = scope_query_to_company(User.query.filter_by(active=True), User).order_by(User.username).all()

    return render_template(
        "caja/index.html",
        company_name=(company.name if company else "Mi comercio"),
        is_admin=_is_admin(),
        open_session=open_session,
        session_summary=session_summary,
        sessions=sessions,
        movements=movements,
        employees=employees,
        status_filter=status_filter,
        selected_user_id=user_filter,
        search_query=request.args.get("q", ""),
    )


@bp.route("/exportar-csv")
@tenant_required
def export_cash_csv():
    from app import CashSession, scope_query_to_company

    if not _is_admin():
        abort(403)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Usuario", "Estado", "Apertura", "Cierre", "Esperado", "Diferencia", "Apertura fecha", "Cierre fecha", "Nota"])
    for session in scope_query_to_company(CashSession.query, CashSession).order_by(CashSession.opened_at.desc()).all():
        summary = _session_summary(session)
        writer.writerow([
            session.id,
            session.user.name if session.user else session.user_id,
            session.status,
            f"{summary['opening']:.2f}",
            f"{_to_decimal(session.counted_amount) if session.counted_amount is not None else Decimal('0.00'):.2f}",
            f"{summary['expected']:.2f}",
            f"{(summary['difference'] if summary['difference'] is not None else Decimal('0.00')):.2f}",
            session.opened_at.strftime("%Y-%m-%d %H:%M") if session.opened_at else "",
            session.closed_at.strftime("%Y-%m-%d %H:%M") if session.closed_at else "",
            session.note or session.closing_note or "",
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="caja_{current_user.company_id or "global"}.csv"'
    return response


@bp.route("/imprimir/<int:session_id>")
@tenant_required
def print_session(session_id):
    session = _get_session_or_404(session_id)
    response = make_response(_session_ticket_text(session))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response
