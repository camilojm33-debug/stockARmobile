"""Modulo de caja: apertura, movimientos, arqueo, cierre y administracion."""

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO, StringIO
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app import tenant_required
from services.sales_calculation_service import is_confirmed_sale_status, sale_payment_breakdown
from stockarmobile.helpers.money import safe_decimal
from stockarmobile.helpers.numbers import safe_float

bp = Blueprint("cash", __name__)


def _to_float(value, default=0.0):
    return safe_float(value, default)


def _to_decimal(value, default="0.00"):
    return safe_decimal(value, default)


def _format_decimal(value):
    return _to_decimal(value)


def _is_admin():
    return getattr(current_user, "role", None) == "admin"


def _company_timezone_name(company):
    return (getattr(company, "timezone", None) or "America/Argentina/Buenos_Aires").strip() or "America/Argentina/Buenos_Aires"


def _resolve_timezone(tz_name):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=-3))


def _utc_naive_to_local_date(value, tz_name):
    if value is None:
        return None
    tz = _resolve_timezone(tz_name)
    return value.replace(tzinfo=timezone.utc).astimezone(tz).date()


def _local_today_date(tz_name):
    tz = _resolve_timezone(tz_name)
    return datetime.now(timezone.utc).astimezone(tz).date()


def _base_session_query():
    from app import CashSession, scope_query_to_company

    query = scope_query_to_company(CashSession.query, CashSession)
    from stockarmobile.branch_context import get_active_branch_id
    active_branch_id = get_active_branch_id()
    if active_branch_id is not None:
        query = query.filter(CashSession.branch_id == active_branch_id)
    if not _is_admin():
        query = query.filter(CashSession.user_id == current_user.id)
    return query


def _base_movement_query():
    from app import CashMovement, CashSession, scope_query_to_company

    query = scope_query_to_company(CashMovement.query, CashMovement)
    from stockarmobile.branch_context import get_active_branch_id
    active_branch_id = get_active_branch_id()
    if active_branch_id is not None:
        query = query.filter(CashMovement.branch_id == active_branch_id)
    if not _is_admin():
        query = query.join(CashSession, CashSession.id == CashMovement.session_id).filter(CashSession.user_id == current_user.id)
    return query


def _current_open_session():
    from app import CashSession

    return _base_session_query().filter(CashSession.status == "abierta", CashSession.user_id == current_user.id).order_by(CashSession.opened_at.desc()).first()


def _session_summary(session):
    sales = [sale for sale in (list(getattr(session, "sales", []) or [])) if is_confirmed_sale_status(getattr(sale, "status", None))]
    movements = list(getattr(session, "movements", []) or [])
    opening = _to_decimal(session.opening_amount)

    payment_totals = {
        "efectivo": Decimal("0.00"),
        "mercado_pago": Decimal("0.00"),
        "debito": Decimal("0.00"),
        "credito": Decimal("0.00"),
        "transferencia": Decimal("0.00"),
        "otros": Decimal("0.00"),
    }
    total_sold = Decimal("0.00")
    for sale in sales:
        total_sold += _to_decimal(getattr(sale, "total_amount", 0))
        for method_key, amount in sale_payment_breakdown(sale).items():
            payment_totals[method_key] = payment_totals.get(method_key, Decimal("0.00")) + _to_decimal(amount)

    income = Decimal("0.00")
    expense = Decimal("0.00")
    withdrawals = Decimal("0.00")
    for movement in movements:
        movement_amount = _to_decimal(movement.amount)
        movement_type = (movement.movement_type or "").strip().lower()
        movement_category = (movement.category or "").strip().lower()
        is_sale_auto_movement = bool(movement.sale_id) or movement_category == "venta"
        if movement_type == "retiro":
            withdrawals += movement_amount
            continue
        if movement_type in {"egreso", "salida", "gasto"}:
            expense += movement_amount
            continue
        if movement_type == "ingreso" and not is_sale_auto_movement:
            income += movement_amount

    expected = opening + payment_totals["efectivo"] + income - expense - withdrawals
    counted = _to_decimal(session.counted_amount) if session.counted_amount is not None else None
    closing_amount = _to_decimal(session.closing_amount) if session.closing_amount is not None else counted
    difference = _to_decimal(session.difference_amount) if session.difference_amount is not None else (counted - expected if counted is not None else None)
    opening_vs_closing_difference = (closing_amount - opening) if closing_amount is not None else None
    return {
        "opening": opening,
        "sales_cash": payment_totals["efectivo"],
        "sales_mp": payment_totals["mercado_pago"],
        "sales_debit": payment_totals["debito"],
        "sales_credit": payment_totals["credito"],
        "sales_transfer": payment_totals["transferencia"],
        "sales_other": payment_totals["otros"],
        "total_sold": total_sold,
        "income": income,
        "expense": expense,
        "withdrawals": withdrawals,
        "total_cash": opening + payment_totals["efectivo"] + income,
        "expected": expected,
        "closing_amount": closing_amount,
        "counted": counted,
        "difference": difference,
        "opening_vs_closing_difference": opening_vs_closing_difference,
    }


def _build_cash_close_rows(session):
    summary = _session_summary(session)
    return [
        ("Monto inicial", summary["opening"]),
        ("Ventas efectivo", summary["sales_cash"]),
        ("Ventas Mercado Pago", summary["sales_mp"]),
        ("Ventas débito", summary["sales_debit"]),
        ("Ventas crédito", summary["sales_credit"]),
        ("Ventas transferencia", summary["sales_transfer"]),
        ("Ventas otros/tarjeta", summary["sales_other"]),
        ("Ingresos", summary["income"]),
        ("Egresos", summary["expense"]),
        ("Retiros", summary["withdrawals"]),
        ("Total vendido", summary["total_sold"]),
        ("Efectivo esperado", summary["expected"]),
    ]


def _get_session_or_404(session_id):
    from app import CashSession, scope_query_to_company

    # Lock the cash-session row during write operations so two computers cannot
    # close/reopen/void the same session concurrently. PostgreSQL enforces the
    # row lock until this request's transaction finishes.
    session_query = scope_query_to_company(CashSession.query, CashSession).filter(CashSession.id == session_id)
    from stockarmobile.branch_context import get_active_branch_id
    active_branch_id = get_active_branch_id()
    if active_branch_id is not None:
        session_query = session_query.filter(CashSession.branch_id == active_branch_id)
    session = session_query.with_for_update().first_or_404()
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
    if summary["opening_vs_closing_difference"] is not None:
        lines.append(f"Dif. apertura/cierre: ${summary['opening_vs_closing_difference']:.2f}")
    lines.append("-" * 32)
    for movement in session.movements:
        lines.append(f"{movement.created_at:%Y-%m-%d %H:%M} | {movement.movement_type} | {movement.category or '-'} | ${movement.amount:.2f}")
    return "\n".join(lines)


@bp.route("/", methods=["GET", "POST"])
@tenant_required
def index():
    from app import CashMovement, CashRegister, CashSession, Company, Sale, User, db, record_audit, scope_query_to_company, utcnow

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
    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    company_tz = _company_timezone_name(company)
    stale_open_session = None
    if (
        open_session
        and open_session.opened_at
        and _utc_naive_to_local_date(open_session.opened_at, company_tz) < _local_today_date(company_tz)
    ):
        stale_open_session = open_session

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        session_id = request.form.get("session_id", type=int)
        target_session = _get_session_or_404(session_id) if session_id else (_get_session_or_404(open_session.id) if open_session else None)

        if action == "open":
            if open_session is not None:
                flash("Ya tienes una caja abierta en esta sucursal.", "warning")
            else:
                from stockarmobile.branch_context import get_active_branch_id
                from cash_treasury import ensure_default_cash_register, _record_session_event

                company_id = getattr(current_user, "company_id", None)
                branch_id = get_active_branch_id()
                register = None
                if branch_id is not None:
                    requested_register_id = request.form.get("cash_register_id", type=int)
                    if requested_register_id:
                        register = (
                            CashRegister.query.filter_by(
                                id=requested_register_id,
                                company_id=company_id,
                                branch_id=branch_id,
                                active=True,
                            )
                            .with_for_update()
                            .first()
                        )
                        if register is None:
                            abort(404)
                    else:
                        register = ensure_default_cash_register(
                            company_id=company_id,
                            branch_id=branch_id,
                        )
                    if (
                        getattr(register, "assigned_user_id", None)
                        and int(register.assigned_user_id) != int(current_user.id)
                        and not _is_admin()
                    ):
                        abort(403)
                    occupied = (
                        CashSession.query.filter_by(
                            company_id=company_id,
                            cash_register_id=register.id,
                            status="abierta",
                        )
                        .with_for_update()
                        .first()
                    )
                    if occupied is not None:
                        flash(
                            f"{register.name} ya está ocupada por otra sesión. Elegí otra caja o cerrá la sesión activa.",
                            "warning",
                        )
                        return redirect(url_for("cash.index"))
                opening_amount = _to_decimal(request.form.get("opening_amount"))
                if opening_amount < 0:
                    flash("El fondo inicial no puede ser negativo.", "danger")
                    return redirect(url_for("cash.index"))
                target_session = CashSession(
                    user_id=current_user.id,
                    company_id=company_id,
                    branch_id=branch_id,
                    cash_register_id=(register.id if register else None),
                    opened_by_user_id=current_user.id,
                    opening_amount=opening_amount,
                    expected_amount=opening_amount,
                    note=request.form.get("note"),
                    status="abierta",
                )
                db.session.add(target_session)
                db.session.flush()
                _record_session_event(
                    target_session,
                    "apertura",
                    note=target_session.note,
                    summary=_session_summary(target_session),
                    payload='{"register_id":%d}' % int(register.id) if register else None,
                )
                record_audit(
                    action="cash_open",
                    entity="cash_session",
                    entity_id=target_session.id,
                    detail=f"Apertura de caja física {register.name if register else 'legacy'}",
                )
                db.session.commit()
                flash("Caja abierta correctamente.", "success")
        elif action == "movement":
            if target_session is None or target_session.status != "abierta":
                flash("Debes tener una caja abierta para registrar movimientos.", "warning")
            else:
                amount = _to_decimal(request.form.get("amount"))
                if amount <= 0:
                    flash("El movimiento debe tener un importe positivo.", "danger")
                else:
                    movement_type = (request.form.get("movement_type") or "ingreso").strip().lower()
                    if movement_type not in {"ingreso", "egreso", "retiro"}:
                        flash("El tipo de movimiento no es válido.", "danger")
                    else:
                        db.session.add(
                            CashMovement(
                                session_id=target_session.id,
                                user_id=current_user.id,
                                company_id=getattr(current_user, "company_id", None),
                                branch_id=getattr(target_session, "branch_id", None),
                                movement_type=movement_type,
                                category=request.form.get("category"),
                                amount=amount,
                                description=request.form.get("description"),
                            )
                        )
                        db.session.flush()
                        from cash_treasury import _record_session_event
                        db.session.expire(target_session, ["movements"])
                        _record_session_event(
                            target_session,
                            f"movimiento_{movement_type}",
                            note=request.form.get("description") or f"Movimiento manual {movement_type} por $ {amount}",
                            summary=_session_summary(target_session),
                            payload='{"category":"manual"}',
                        )
                        record_audit(action="cash_movement", entity="cash_movement", detail=f"Movimiento {movement_type} por {amount}")
                        db.session.commit()
                        flash("Movimiento registrado.", "success")
        elif action == "close":
            if target_session is None or target_session.status != "abierta":
                flash("No hay una caja abierta para cerrar.", "warning")
            else:
                summary = _session_summary(target_session)
                counted_amount = _to_decimal(request.form.get("counted_amount") or request.form.get("closing_amount") or summary["expected"])
                if counted_amount < 0:
                    flash("El efectivo contado no puede ser negativo.", "danger")
                    return redirect(url_for("cash.index"))
                opening_vs_closing_difference = counted_amount - summary["opening"]
                from cash_treasury import _record_session_event
                _record_session_event(
                    target_session,
                    "cierre",
                    note=request.form.get("closing_note"),
                    summary={
                        **summary,
                        "counted": counted_amount,
                        "difference": counted_amount - summary["expected"],
                    },
                )
                target_session.closed_at = utcnow()
                target_session.expected_amount = summary["expected"]
                target_session.counted_amount = counted_amount
                target_session.closing_amount = counted_amount
                target_session.difference_amount = counted_amount - summary["expected"]
                target_session.closing_note = request.form.get("closing_note")
                target_session.status = "cerrada"
                record_audit(action="cash_close", entity="cash_session", entity_id=target_session.id, detail=f"Cierre de caja diferencia={target_session.difference_amount}")
                db.session.commit()
                flash(
                    f"Caja cerrada. Apertura ${summary['opening']:.2f} | Cierre ${counted_amount:.2f} | Diferencia apertura/cierre ${opening_vs_closing_difference:.2f}",
                    "success",
                )
        elif action == "notify_admin":
            if target_session is None or target_session.status != "abierta":
                flash("No hay caja abierta para notificar.", "warning")
            else:
                record_audit(
                    action="cash_notify_admin",
                    entity="cash_session",
                    entity_id=target_session.id,
                    detail="Empleado notificó caja abierta pendiente de cierre.",
                )
                db.session.commit()
                flash("Aviso enviado al administrador.", "info")
        elif action == "reopen":
            if not _is_admin():
                abort(403)
            if target_session is None or target_session.status != "cerrada":
                flash("Seleccioná una sesión cerrada para abrir un nuevo turno.", "warning")
            else:
                from cash_treasury import _record_session_event, ensure_default_cash_register

                register = None
                if target_session.branch_id is not None:
                    register = (
                        CashRegister.query.filter_by(
                            id=target_session.cash_register_id,
                            company_id=getattr(current_user, "company_id", None),
                            branch_id=target_session.branch_id,
                            active=True,
                        )
                        .with_for_update()
                        .first()
                        if target_session.cash_register_id
                        else ensure_default_cash_register(
                            company_id=getattr(current_user, "company_id", None),
                            branch_id=target_session.branch_id,
                        )
                    )
                    if register is None:
                        flash("La caja física original ya no está disponible. No se reabrió la sesión.", "danger")
                        return redirect(url_for("cash.index"))
                    occupied = (
                        CashSession.query.filter_by(
                            company_id=target_session.company_id,
                            cash_register_id=register.id,
                            status="abierta",
                        )
                        .with_for_update()
                        .first()
                    )
                    if occupied is not None:
                        flash(f"{register.name} ya tiene una sesión abierta. No se puede iniciar otro turno en ese puesto.", "warning")
                        return redirect(url_for("cash.index"))

                previous_summary = _session_summary(target_session)
                _record_session_event(
                    target_session,
                    "reapertura_solicitada",
                    note=(request.form.get("reopen_reason") or "Reapertura autorizada"),
                    summary=previous_summary,
                    payload='{"previous_status":"cerrada","new_session_required":true}',
                )
                opening_balance = (
                    _to_decimal(target_session.counted_amount)
                    if target_session.counted_amount is not None
                    else _to_decimal(target_session.expected_amount or previous_summary["expected"])
                )
                new_session = CashSession(
                    user_id=current_user.id,
                    company_id=target_session.company_id,
                    branch_id=target_session.branch_id,
                    cash_register_id=(register.id if register else None),
                    opened_by_user_id=current_user.id,
                    opening_amount=opening_balance,
                    expected_amount=opening_balance,
                    note=f"Reapertura de turno originada en sesión #{target_session.id}",
                    status="abierta",
                )
                db.session.add(new_session)
                db.session.flush()
                _record_session_event(
                    new_session,
                    "apertura_reapertura",
                    note=f"Nuevo turno a partir de la sesión cerrada #{target_session.id}",
                    summary=_session_summary(new_session),
                    payload='{"previous_session_id":%d}' % int(target_session.id),
                )
                record_audit(
                    action="cash_reopen",
                    entity="cash_session",
                    entity_id=new_session.id,
                    detail=f"Nuevo turno basado en la sesión cerrada #{target_session.id}",
                )
                db.session.commit()
                flash(f"Se abrió una nueva sesión #{new_session.id}. La sesión #{target_session.id} conserva su cierre original.", "success")
        elif action == "void":
            if not _is_admin():
                abort(403)
            if target_session is None:
                flash("Selecciona una caja para anular.", "warning")
            else:
                from cash_treasury import _record_session_event
                _record_session_event(
                    target_session,
                    "anulacion",
                    note=(request.form.get("void_reason") or "Anulación administrativa"),
                    summary=_session_summary(target_session),
                )
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
    session_summaries = {session.id: _session_summary(session) for session in sessions}
    pending_comprobantes = []
    pending_comprobantes_total = Decimal("0.00")
    if open_session:
        pending_query = (
            scope_query_to_company(Sale.query, Sale)
            .filter(
                Sale.cash_session_id == open_session.id,
                Sale.requiere_comprobante.is_(True),
                Sale.comprobante_emitido.is_(False),
            )
            .order_by(Sale.date.desc())
        )
        pending_comprobantes = pending_query.all()
        for pending_sale in pending_comprobantes:
            pending_comprobantes_total += _to_decimal(getattr(pending_sale, "total_amount", 0))

    cash_registers = []
    from stockarmobile.branch_context import get_active_branch_id
    active_branch_id = get_active_branch_id()
    if active_branch_id is not None:
        register_query = CashRegister.query.filter_by(
            company_id=getattr(current_user, "company_id", None),
            branch_id=active_branch_id,
            active=True,
        )
        if not _is_admin():
            register_query = register_query.filter(
                (CashRegister.assigned_user_id.is_(None))
                | (CashRegister.assigned_user_id == current_user.id)
            )
        cash_registers = register_query.order_by(CashRegister.is_default.desc(), CashRegister.name.asc()).all()

    employees = []
    if _is_admin():
        from stockarmobile.company_memberships import get_company_users
        employees = get_company_users(getattr(current_user, "company_id", None), active_only=True)

    return render_template(
        "caja/index.html",
        company_name=(company.name if company else "Mi comercio"),
        is_admin=_is_admin(),
        open_session=open_session,
        session_summary=session_summary,
        session_summaries=session_summaries,
        sessions=sessions,
        movements=movements,
        employees=employees,
        cash_registers=cash_registers,
        status_filter=status_filter,
        selected_user_id=user_filter,
        search_query=request.args.get("q", ""),
        stale_open_session=stale_open_session,
        close_rows=_build_cash_close_rows(open_session) if open_session else [],
        pending_comprobantes=pending_comprobantes,
        pending_comprobantes_count=len(pending_comprobantes),
        pending_comprobantes_total=pending_comprobantes_total,
    )


@bp.route("/exportar-csv")
@tenant_required
def export_cash_csv():
    from app import Branch, CashRegister, CashSession, scope_query_to_company

    if not _is_admin():
        abort(403)

    output = StringIO()
    writer = csv.writer(output)
    totals = {
        "sales_cash": Decimal("0.00"),
        "sales_transfer": Decimal("0.00"),
        "sales_debit": Decimal("0.00"),
        "sales_credit": Decimal("0.00"),
        "sales_mp": Decimal("0.00"),
        "sales_other": Decimal("0.00"),
    }
    writer.writerow([
        "ID",
        "Sucursal",
        "Código sucursal",
        "Caja física",
        "Código caja",
        "Usuario",
        "Estado",
        "Apertura",
        "Ventas efectivo",
        "Ventas transferencia",
        "Ventas debito",
        "Ventas credito",
        "Ventas MP",
        "Ventas otros",
        "Cierre",
        "Esperado",
        "Diferencia",
        "Apertura fecha",
        "Cierre fecha",
        "Nota",
    ])
    sessions = _base_session_query().order_by(CashSession.opened_at.desc()).all()
    branch_ids = {int(row.branch_id) for row in sessions if row.branch_id}
    register_ids = {int(row.cash_register_id) for row in sessions if row.cash_register_id}
    branch_map = {int(row.id): row for row in Branch.query.filter(Branch.id.in_(branch_ids)).all()} if branch_ids else {}
    register_map = {int(row.id): row for row in CashRegister.query.filter(CashRegister.id.in_(register_ids)).all()} if register_ids else {}
    for session in sessions:
        summary = _session_summary(session)
        branch = branch_map.get(int(session.branch_id)) if session.branch_id else None
        register = register_map.get(int(session.cash_register_id)) if session.cash_register_id else None
        totals["sales_cash"] += summary["sales_cash"]
        totals["sales_transfer"] += summary["sales_transfer"]
        totals["sales_debit"] += summary["sales_debit"]
        totals["sales_credit"] += summary["sales_credit"]
        totals["sales_mp"] += summary["sales_mp"]
        totals["sales_other"] += summary["sales_other"]
        writer.writerow([
            session.id,
            branch.name if branch else "",
            branch.code if branch else "",
            register.name if register else "",
            register.code if register else "",
            session.user.name if session.user else session.user_id,
            session.status,
            f"{summary['opening']:.2f}",
            f"{summary['sales_cash']:.2f}",
            f"{summary['sales_transfer']:.2f}",
            f"{summary['sales_debit']:.2f}",
            f"{summary['sales_credit']:.2f}",
            f"{summary['sales_mp']:.2f}",
            f"{summary['sales_other']:.2f}",
            f"{_to_decimal(session.counted_amount) if session.counted_amount is not None else Decimal('0.00'):.2f}",
            f"{summary['expected']:.2f}",
            f"{(summary['difference'] if summary['difference'] is not None else Decimal('0.00')):.2f}",
            session.opened_at.strftime("%Y-%m-%d %H:%M") if session.opened_at else "",
            session.closed_at.strftime("%Y-%m-%d %H:%M") if session.closed_at else "",
            session.note or session.closing_note or "",
        ])

    writer.writerow([])
    writer.writerow([
        "TOTAL CONSOLIDADO METODOS",
        "",
        "",
        "",
        f"{totals['sales_cash']:.2f}",
        f"{totals['sales_transfer']:.2f}",
        f"{totals['sales_debit']:.2f}",
        f"{totals['sales_credit']:.2f}",
        f"{totals['sales_mp']:.2f}",
        f"{totals['sales_other']:.2f}",
        "",
        "",
        "",
        "",
        "",
        "",
    ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="caja_{current_user.company_id or "global"}.csv"'
    return response


@bp.route("/exportar-excel")
@tenant_required
def export_cash_excel():
    from app import Branch, CashRegister, CashSession, scope_query_to_company

    if not _is_admin():
        abort(403)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cajas"
    totals = {
        "sales_cash": Decimal("0.00"),
        "sales_transfer": Decimal("0.00"),
        "sales_debit": Decimal("0.00"),
        "sales_credit": Decimal("0.00"),
        "sales_mp": Decimal("0.00"),
        "sales_other": Decimal("0.00"),
    }
    sheet.append([
        "ID",
        "Sucursal",
        "Código sucursal",
        "Caja física",
        "Código caja",
        "Usuario",
        "Estado",
        "Monto inicial",
        "Ventas efectivo",
        "Ventas MP",
        "Ventas débito",
        "Ventas crédito",
        "Ventas transferencia",
        "Ventas otros",
        "Ingresos",
        "Egresos",
        "Retiros",
        "Total vendido",
        "Esperado",
        "Contado",
        "Diferencia",
        "Apertura",
        "Cierre",
    ])

    sessions = _base_session_query().order_by(CashSession.opened_at.desc()).all()
    branch_ids = {int(row.branch_id) for row in sessions if row.branch_id}
    register_ids = {int(row.cash_register_id) for row in sessions if row.cash_register_id}
    branch_map = {int(row.id): row for row in Branch.query.filter(Branch.id.in_(branch_ids)).all()} if branch_ids else {}
    register_map = {int(row.id): row for row in CashRegister.query.filter(CashRegister.id.in_(register_ids)).all()} if register_ids else {}
    for session in sessions:
        summary = _session_summary(session)
        branch = branch_map.get(int(session.branch_id)) if session.branch_id else None
        register = register_map.get(int(session.cash_register_id)) if session.cash_register_id else None
        totals["sales_cash"] += summary["sales_cash"]
        totals["sales_transfer"] += summary["sales_transfer"]
        totals["sales_debit"] += summary["sales_debit"]
        totals["sales_credit"] += summary["sales_credit"]
        totals["sales_mp"] += summary["sales_mp"]
        totals["sales_other"] += summary["sales_other"]
        sheet.append([
            session.id,
            branch.name if branch else "",
            branch.code if branch else "",
            register.name if register else "",
            register.code if register else "",
            session.user.name if session.user else session.user_id,
            session.status,
            float(summary["opening"]),
            float(summary["sales_cash"]),
            float(summary["sales_mp"]),
            float(summary["sales_debit"]),
            float(summary["sales_credit"]),
            float(summary["sales_transfer"]),
            float(summary["sales_other"]),
            float(summary["income"]),
            float(summary["expense"]),
            float(summary["withdrawals"]),
            float(summary["total_sold"]),
            float(summary["expected"]),
            float(summary["counted"] or 0),
            float(summary["difference"] or 0),
            session.opened_at.strftime("%Y-%m-%d %H:%M") if session.opened_at else "",
            session.closed_at.strftime("%Y-%m-%d %H:%M") if session.closed_at else "",
        ])

    sheet.append([])
    sheet.append([
        "TOTAL CONSOLIDADO METODOS",
        "",
        "",
        "",
        float(totals["sales_cash"]),
        float(totals["sales_mp"]),
        float(totals["sales_debit"]),
        float(totals["sales_credit"]),
        float(totals["sales_transfer"]),
        float(totals["sales_other"]),
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ])

    binary = BytesIO()
    workbook.save(binary)
    binary.seek(0)
    response = make_response(binary.getvalue())
    response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response.headers["Content-Disposition"] = f'attachment; filename="caja_{current_user.company_id or "global"}.xlsx"'
    return response


@bp.route("/exportar-pdf")
@tenant_required
def export_cash_pdf():
    from app import Branch, CashRegister, CashSession, scope_query_to_company

    if not _is_admin():
        abort(403)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _, page_h = A4
    y = page_h - 40
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(36, y, "StockArmobile - Administración de Cajas")
    y -= 20
    pdf.setFont("Helvetica", 9)
    totals = {
        "sales_cash": Decimal("0.00"),
        "sales_transfer": Decimal("0.00"),
        "sales_debit": Decimal("0.00"),
        "sales_credit": Decimal("0.00"),
        "sales_mp": Decimal("0.00"),
        "sales_other": Decimal("0.00"),
    }

    sessions = _base_session_query().order_by(CashSession.opened_at.desc()).limit(100).all()
    branch_ids = {int(row.branch_id) for row in sessions if row.branch_id}
    register_ids = {int(row.cash_register_id) for row in sessions if row.cash_register_id}
    branch_map = {int(row.id): row for row in Branch.query.filter(Branch.id.in_(branch_ids)).all()} if branch_ids else {}
    register_map = {int(row.id): row for row in CashRegister.query.filter(CashRegister.id.in_(register_ids)).all()} if register_ids else {}
    for session in sessions:
        summary = _session_summary(session)
        branch = branch_map.get(int(session.branch_id)) if session.branch_id else None
        register = register_map.get(int(session.cash_register_id)) if session.cash_register_id else None
        totals["sales_cash"] += summary["sales_cash"]
        totals["sales_transfer"] += summary["sales_transfer"]
        totals["sales_debit"] += summary["sales_debit"]
        totals["sales_credit"] += summary["sales_credit"]
        totals["sales_mp"] += summary["sales_mp"]
        totals["sales_other"] += summary["sales_other"]
        branch_label = f"{branch.name} ({branch.code})" if branch else "Sin sucursal"
        register_label = f"{register.name} ({register.code})" if register else "Caja histórica"
        line = f"Caja #{session.id} | {branch_label} | {register_label} | {session.user.name if session.user else session.user_id} | {session.status}"
        breakdown = (
            f"EF ${summary['sales_cash']:.2f} | TR ${summary['sales_transfer']:.2f} | DB ${summary['sales_debit']:.2f} | "
            f"CR ${summary['sales_credit']:.2f} | MP ${summary['sales_mp']:.2f} | OT ${summary['sales_other']:.2f}"
        )
        totals_line = f"Esperado ${summary['expected']:.2f} | Diferencia ${((summary['difference'] or Decimal('0.00'))):.2f}"
        if y < 40:
            pdf.showPage()
            y = page_h - 40
            pdf.setFont("Helvetica", 9)
        pdf.drawString(36, y, line[:180])
        y -= 14
        if y < 40:
            pdf.showPage()
            y = page_h - 40
            pdf.setFont("Helvetica", 9)
        pdf.drawString(36, y, breakdown[:180])
        y -= 14
        if y < 40:
            pdf.showPage()
            y = page_h - 40
            pdf.setFont("Helvetica", 9)
        pdf.drawString(36, y, totals_line[:180])
        y -= 18

    if y < 80:
        pdf.showPage()
        y = page_h - 40
        pdf.setFont("Helvetica", 9)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(36, y, "TOTAL CONSOLIDADO DE METODOS")
    y -= 14
    pdf.setFont("Helvetica", 9)
    pdf.drawString(
        36,
        y,
        (
            f"EF ${totals['sales_cash']:.2f} | TR ${totals['sales_transfer']:.2f} | DB ${totals['sales_debit']:.2f} | "
            f"CR ${totals['sales_credit']:.2f} | MP ${totals['sales_mp']:.2f} | OT ${totals['sales_other']:.2f}"
        )[:180],
    )

    pdf.save()
    buffer.seek(0)
    response = make_response(buffer.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="caja_{current_user.company_id or "global"}.pdf"'
    return response


@bp.route("/imprimir/<int:session_id>")
@tenant_required
def print_session(session_id):
    session = _get_session_or_404(session_id)
    response = make_response(_session_ticket_text(session))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response
