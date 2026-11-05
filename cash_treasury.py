"""Tesorería centralizada y operación segura de caja multisucursal."""

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app import (
    Branch,
    CashMovement,
    CashRegister,
    CashSession,
    CashSessionEvent,
    CashTransfer,
    FinancialAccount,
    FinancialAccountMovement,
    User,
    db,
    record_audit,
    tenant_required,
    utcnow,
)
from cash import _session_summary, _to_decimal

bp = Blueprint("cash_treasury", __name__)


def _company_id():
    value = getattr(current_user, "company_id", None)
    return int(value) if value else None


def _require_admin():
    if getattr(current_user, "role", None) not in {"admin", "superadmin"}:
        abort(403)


def _money(value, *, allow_negative=False):
    raw = str(value if value is not None else "0").strip().replace(",", ".")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Ingresá un importe válido.") from None
    if not amount.is_finite():
        raise ValueError("Ingresá un importe válido.")
    if not allow_negative and amount < 0:
        raise ValueError("El importe no puede ser negativo.")
    return amount


def _record_session_event(session_row, event_type, *, note=None, summary=None, payload=None):
    summary = summary or {}
    event = CashSessionEvent(
        company_id=int(session_row.company_id),
        branch_id=getattr(session_row, "branch_id", None),
        cash_session_id=int(session_row.id),
        event_type=str(event_type),
        actor_user_id=getattr(current_user, "id", None),
        opening_amount=summary.get("opening"),
        expected_amount=summary.get("expected"),
        counted_amount=summary.get("counted"),
        difference_amount=summary.get("difference"),
        note=(note or "")[:2000] or None,
        payload_json=payload,
    )
    db.session.add(event)
    db.session.flush()
    return event


def ensure_default_cash_register(*, company_id, branch_id):
    """Return the branch default register, creating it under a branch row lock."""
    branch = (
        Branch.query.filter_by(
            id=int(branch_id),
            company_id=int(company_id),
            active=True,
        )
        .with_for_update()
        .first()
    )
    if branch is None:
        raise ValueError("La sucursal no existe, está inactiva o no pertenece a la empresa.")
    register = (
        CashRegister.query.filter_by(
            company_id=int(company_id),
            branch_id=int(branch_id),
            is_default=True,
            active=True,
        )
        .order_by(CashRegister.id.asc())
        .first()
    )
    if register is not None:
        return register

    register = CashRegister(
        company_id=int(company_id),
        branch_id=int(branch_id),
        name="Caja 1",
        code="CAJA-1",
        active=True,
        is_default=True,
        created_by_user_id=getattr(current_user, "id", None),
    )
    db.session.add(register)
    db.session.flush()
    return register


def financial_account_balance(account):
    """Calculate a ledger balance without mutating the account or its movements."""
    balance = _to_decimal(getattr(account, "opening_balance", 0))
    for movement in list(getattr(account, "movements", []) or []):
        movement_type = str(getattr(movement, "movement_type", "") or "").lower()
        amount = _to_decimal(getattr(movement, "amount", 0))
        if movement_type in {"apertura", "ingreso"}:
            balance += amount
        elif movement_type == "egreso":
            balance -= amount
        elif movement_type == "ajuste":
            balance += amount
    return balance.quantize(Decimal("0.01"))

def record_sale_financial_movements(sale, payment_breakdown, *, posting_revision=None):
    """Post configured non-cash sale portions into their mapped account ledgers.

    Missing mappings never block a sale: only accounts explicitly associated
    with the payment method receive automatic entries. The per-sale/method key
    prevents duplicate ledger posts on retries.
    """
    company_id = getattr(sale, "company_id", None)
    if not company_id:
        return []
    posted = []
    method_keys = ("mercado_pago", "debito", "credito", "transferencia", "otros")
    for method_key in method_keys:
        amount = _to_decimal((payment_breakdown or {}).get(method_key, 0))
        if amount <= 0:
            continue
        account_query = FinancialAccount.query.filter(
            FinancialAccount.company_id == int(company_id),
            FinancialAccount.active.is_(True),
            FinancialAccount.payment_method_key == method_key,
        )
        branch_id = getattr(sale, "branch_id", None)
        if branch_id is None:
            account_query = account_query.filter(FinancialAccount.branch_id.is_(None))
        else:
            account_query = account_query.filter(
                (FinancialAccount.branch_id == int(branch_id))
                | (FinancialAccount.branch_id.is_(None))
            )
        account = (
            account_query
            .order_by(FinancialAccount.branch_id.is_(None).asc(), FinancialAccount.id.asc())
            .with_for_update()
            .first()
        )
        if account is None:
            continue
        revision_key = str(posting_revision or "").strip()
        idempotency_key = (
            f"sale:{int(sale.id)}:edit:{revision_key}:{method_key}"
            if revision_key else f"sale:{int(sale.id)}:{method_key}"
        )
        exists = FinancialAccountMovement.query.filter_by(
            company_id=int(company_id),
            idempotency_key=idempotency_key,
        ).first()
        if exists is not None:
            continue
        movement = FinancialAccountMovement(
            company_id=int(company_id),
            branch_id=getattr(sale, "branch_id", None),
            account_id=int(account.id),
            movement_type="ingreso",
            amount=amount,
            description=f"Venta #{sale.id} · {method_key.replace('_', ' ')}",
            source_type="sale_edit" if posting_revision else "sale",
            source_id=int(sale.id),
            idempotency_key=idempotency_key,
            created_by_user_id=getattr(sale, "seller_id", None),
        )
        db.session.add(movement)
        posted.append(movement)
    if posted:
        db.session.flush()
    return posted


def reverse_sale_financial_movements(sale, *, reason="anulada"):
    """Append compensating ledger entries for every existing non-cash sale posting.

    Original ledger rows remain immutable. A stable idempotency key guarantees
    that retries of the same cancellation/edit cannot reverse an entry twice.
    """
    company_id = getattr(sale, "company_id", None)
    sale_id = getattr(sale, "id", None)
    if not company_id or not sale_id:
        return []
    originals = (
        FinancialAccountMovement.query
        .filter(
            FinancialAccountMovement.company_id == int(company_id),
            FinancialAccountMovement.source_id == int(sale_id),
            FinancialAccountMovement.source_type.in_(("sale", "sale_edit")),
        )
        .order_by(FinancialAccountMovement.id.asc())
        .all()
    )
    created = []
    for original in originals:
        key = f"sale:{int(sale_id)}:reverse:{int(original.id)}"
        exists = FinancialAccountMovement.query.filter_by(
            company_id=int(company_id),
            idempotency_key=key,
        ).first()
        if exists is not None:
            continue
        reversal = FinancialAccountMovement(
            company_id=int(company_id),
            branch_id=original.branch_id,
            account_id=int(original.account_id),
            movement_type="egreso",
            amount=_to_decimal(original.amount),
            description=f"Reversión venta #{sale_id} · asiento #{original.id} · {str(reason or 'anulada')[:80]}",
            reference=f"Reversa asiento financiero #{original.id}",
            source_type="sale_reversal",
            source_id=int(sale_id),
            idempotency_key=key,
            created_by_user_id=getattr(current_user, "id", None) or getattr(sale, "seller_id", None),
        )
        db.session.add(reversal)
        created.append(reversal)
    if created:
        db.session.flush()
    return created


def reconcile_sale_financial_movements(sale, payment_breakdown, *, revision_id):
    """Reverse the previous sale postings, then post the edited payment split."""
    revision_key = str(revision_id or "").strip()
    if not revision_key:
        raise ValueError("La revisión de la venta es obligatoria para conciliar la tesorería.")
    reversed_rows = reverse_sale_financial_movements(sale, reason=f"edición {revision_key}")
    posted_rows = record_sale_financial_movements(
        sale,
        payment_breakdown,
        posting_revision=revision_key,
    )
    return {"reversed": reversed_rows, "posted": posted_rows}



def _get_company_branch(branch_id, *, active=True, lock=False):
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        abort(404)
    query = Branch.query.filter_by(id=branch_id, company_id=_company_id())
    if active:
        query = query.filter_by(active=True)
    if lock:
        query = query.with_for_update()
    branch = query.first()
    if branch is None:
        abort(404)
    return branch


def _get_transfer_or_404(transfer_id):
    return (
        CashTransfer.query.filter_by(id=int(transfer_id), company_id=_company_id())
        .with_for_update()
        .first_or_404()
    )


def _audit(action, entity, entity_id, detail):
    record_audit(
        action=action,
        entity=entity,
        entity_id=entity_id,
        company_id=_company_id(),
        detail=detail,
    )


@bp.route("/", methods=["GET"])
@tenant_required
def index():
    _require_admin()
    company_id = _company_id()
    branches = (
        Branch.query.filter_by(company_id=company_id, active=True)
        .order_by(Branch.is_default.desc(), Branch.name.asc())
        .all()
    )
    branch_rows = []
    open_sessions = (
        CashSession.query.filter_by(company_id=company_id, status="abierta")
        .order_by(CashSession.opened_at.desc())
        .all()
    )
    session_summaries = {int(item.id): _session_summary(item) for item in open_sessions}
    events = (
        CashSessionEvent.query.filter_by(company_id=company_id)
        .order_by(CashSessionEvent.created_at.desc(), CashSessionEvent.id.desc())
        .limit(60)
        .all()
    )
    by_branch = {}
    for cash_session in open_sessions:
        by_branch.setdefault(int(cash_session.branch_id or 0), []).append(cash_session)

    expected_total = Decimal("0.00")
    for branch in branches:
        sessions = by_branch.get(int(branch.id), [])
        summaries = [_session_summary(item) for item in sessions]
        branch_expected = sum((item["expected"] for item in summaries), Decimal("0.00"))
        expected_total += branch_expected
        branch_rows.append(
            {
                "branch": branch,
                "sessions": sessions,
                "open_count": len(sessions),
                "expected_cash": branch_expected,
                "registers": CashRegister.query.filter_by(
                    company_id=company_id, branch_id=branch.id, active=True
                ).order_by(CashRegister.name.asc()).all(),
            }
        )

    transfers = (
        CashTransfer.query.filter_by(company_id=company_id)
        .order_by(CashTransfer.created_at.desc(), CashTransfer.id.desc())
        .limit(40)
        .all()
    )
    accounts = (
        FinancialAccount.query.filter_by(company_id=company_id, active=True)
        .order_by(FinancialAccount.branch_id.asc().nullsfirst(), FinancialAccount.name.asc())
        .all()
    )
    account_rows = [
        {"account": account, "balance": financial_account_balance(account)}
        for account in accounts
    ]
    employees = (
        User.query.filter_by(company_id=company_id, active=True)
        .filter(User.role != "superadmin")
        .order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc())
        .all()
    )
    pending_transfers_count = CashTransfer.query.filter(
        CashTransfer.company_id == company_id,
        CashTransfer.status.in_(["pendiente", "en_transito"]),
    ).count()
    return render_template(
        "caja/tesoreria.html",
        branches=branches,
        branch_rows=branch_rows,
        transfers=transfers,
        accounts=account_rows,
        events=events,
        employees=employees,
        transfer_idempotency_key=uuid4().hex,
        account_movement_idempotency_keys={int(row.id): uuid4().hex for row in accounts},
        open_sessions_count=len(open_sessions),
        session_summaries=session_summaries,
        expected_cash_total=expected_total,
        pending_transfers=pending_transfers_count,
    )


@bp.route("/cajas/crear", methods=["POST"])
@tenant_required
def create_register():
    _require_admin()
    name = (request.form.get("name") or "").strip()[:100]
    code = (request.form.get("code") or "").strip().upper()[:40]
    branch = _get_company_branch(request.form.get("branch_id", type=int), lock=True)
    if len(name) < 2:
        flash("Ingresá un nombre de caja válido.", "danger")
        return redirect(url_for("cash_treasury.index"))
    if not code:
        code = "CAJA-" + uuid4().hex[:8].upper()
    if CashRegister.query.filter_by(
        company_id=_company_id(), branch_id=branch.id, code=code
    ).first():
        flash("Ya existe una caja con ese código en la sucursal.", "warning")
        return redirect(url_for("cash_treasury.index"))

    assigned_user_id = request.form.get("assigned_user_id", type=int)
    if assigned_user_id:
        assigned_user = User.query.filter_by(
            id=assigned_user_id, company_id=_company_id(), active=True
        ).first()
        if assigned_user is None or assigned_user.role == "superadmin":
            flash("El responsable seleccionado no pertenece a esta empresa o está inactivo.", "danger")
            return redirect(url_for("cash_treasury.index"))

    make_default = request.form.get("is_default") == "1"
    if make_default:
        CashRegister.query.filter_by(
            company_id=_company_id(), branch_id=branch.id, is_default=True
        ).update({"is_default": False}, synchronize_session=False)
    register = CashRegister(
        company_id=_company_id(),
        branch_id=branch.id,
        name=name,
        code=code,
        active=True,
        is_default=make_default,
        assigned_user_id=assigned_user_id or None,
        created_by_user_id=current_user.id,
    )
    db.session.add(register)
    try:
        db.session.flush()
        _audit("cash_register_create", "cash_register", register.id, f"Caja {name} creada en {branch.name}")
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Ya existe una caja con ese código o hay otra caja predeterminada en la sucursal.", "warning")
        return redirect(url_for("cash_treasury.index"))
    flash("Caja física creada correctamente.", "success")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/cajas/<int:register_id>/estado", methods=["POST"])
@tenant_required
def toggle_register(register_id):
    _require_admin()
    register = (
        CashRegister.query.filter_by(id=register_id, company_id=_company_id())
        .with_for_update()
        .first_or_404()
    )
    active = request.form.get("active") == "1"
    if not active:
        if register.is_default:
            flash("La caja predeterminada no se puede desactivar. Primero asigná otra como predeterminada.", "warning")
            return redirect(url_for("cash_treasury.index"))
        if CashSession.query.filter_by(cash_register_id=register.id, status="abierta").first():
            flash("No se puede desactivar una caja que tiene una sesión abierta.", "warning")
            return redirect(url_for("cash_treasury.index"))
    register.active = active
    register.updated_at = utcnow()
    _audit("cash_register_update", "cash_register", register.id, f"Estado de caja actualizado a {active}")
    db.session.commit()
    flash("Estado de caja actualizado.", "success")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/transferencias/crear", methods=["POST"])
@tenant_required
def create_transfer():
    _require_admin()
    company_id = _company_id()
    try:
        amount = _money(request.form.get("amount"))
        if amount <= 0:
            raise ValueError("El importe de la transferencia debe ser mayor a cero.")
        source_session_id = request.form.get("source_session_id", type=int)
        source_session = (
            CashSession.query.filter_by(
                id=source_session_id,
                company_id=company_id,
                status="abierta",
            )
            .with_for_update()
            .first()
        )
        if source_session is None:
            raise ValueError("La caja de origen no está abierta.")
        source_register = (
            CashRegister.query.filter_by(
                id=source_session.cash_register_id,
                company_id=company_id,
                branch_id=source_session.branch_id,
                active=True,
            )
            .first()
        )
        if source_register is None:
            raise ValueError("La sesión de origen no tiene una caja física activa asignada.")
        target_branch = _get_company_branch(request.form.get("target_branch_id", type=int), lock=True)
        target_register = (
            CashRegister.query.filter_by(
                id=request.form.get("target_register_id", type=int),
                company_id=company_id,
                branch_id=target_branch.id,
                active=True,
            )
            .first()
        )
        if target_register is None:
            raise ValueError("Seleccioná una caja física activa de la sucursal de destino.")
        if int(target_branch.id) == int(source_register.branch_id):
            raise ValueError("La sucursal de destino debe ser distinta de la sucursal de origen.")
        if int(target_register.id) == int(source_register.id):
            raise ValueError("La caja de destino debe ser distinta de la caja de origen.")
        summary = _session_summary(source_session)
        if amount > summary["expected"]:
            raise ValueError("La transferencia supera el efectivo esperado disponible en la caja de origen.")
        key = (request.form.get("idempotency_key") or "").strip()[:100] or uuid4().hex
        existing = CashTransfer.query.filter_by(company_id=company_id, idempotency_key=key).first()
        if existing is not None:
            flash(f"La transferencia #{existing.id} ya estaba registrada.", "info")
            return redirect(url_for("cash_treasury.index"))
        transfer = CashTransfer(
            company_id=company_id,
            source_branch_id=source_register.branch_id,
            target_branch_id=target_branch.id,
            source_register_id=source_register.id,
            target_register_id=target_register.id,
            source_session_id=source_session.id,
            amount=amount,
            currency="ARS",
            status="pendiente",
            idempotency_key=key,
            description=(request.form.get("description") or "").strip()[:255] or None,
            created_by_user_id=current_user.id,
        )
        db.session.add(transfer)
        db.session.flush()
        _audit("cash_transfer_create", "cash_transfer", transfer.id, f"Transferencia solicitada por $ {amount}")
        db.session.commit()
        flash(f"Transferencia #{transfer.id} creada. Todavía no afecta el efectivo: falta despacharla.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except IntegrityError:
        db.session.rollback()
        flash("La solicitud ya existe o está siendo procesada. Revisá la lista de transferencias antes de reintentar.", "warning")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/transferencias/<int:transfer_id>/despachar", methods=["POST"])
@tenant_required
def dispatch_transfer(transfer_id):
    _require_admin()
    try:
        transfer = _get_transfer_or_404(transfer_id)
        if transfer.status != "pendiente":
            raise ValueError("La transferencia ya no está pendiente de despacho.")
        source_session = (
            CashSession.query.filter_by(
                id=transfer.source_session_id,
                company_id=_company_id(),
                branch_id=transfer.source_branch_id,
                cash_register_id=transfer.source_register_id,
                status="abierta",
            )
            .with_for_update()
            .first()
        )
        if source_session is None:
            raise ValueError("La caja de origen ya no está abierta. Reabrila o generá una nueva solicitud.")
        summary = _session_summary(source_session)
        if _to_decimal(transfer.amount) > summary["expected"]:
            raise ValueError("El efectivo disponible ya no alcanza para despachar la transferencia.")
        movement = CashMovement(
            session_id=source_session.id,
            user_id=current_user.id,
            company_id=_company_id(),
            branch_id=transfer.source_branch_id,
            cash_transfer_id=transfer.id,
            movement_type="retiro",
            category="transferencia_enviada",
            amount=transfer.amount,
            description=f"Transferencia #{transfer.id} hacia {transfer.target_branch.name}",
        )
        db.session.add(movement)
        db.session.flush()
        # _session_summary reads ORM relationship collections. Expire the
        # cached movement list so the event snapshot includes the withdrawal.
        db.session.expire(source_session, ["movements"])
        transfer.status = "en_transito"
        transfer.dispatched_by_user_id = current_user.id
        transfer.dispatched_at = utcnow()
        _record_session_event(
            source_session,
            "transferencia_enviada",
            note=f"Transferencia #{transfer.id} enviada por $ {transfer.amount}",
            summary=_session_summary(source_session),
            payload='{"transfer_id":%d,"direction":"out"}' % int(transfer.id),
        )
        _audit("cash_transfer_dispatch", "cash_transfer", transfer.id, "Transferencia despachada")
        db.session.commit()
        flash(f"Transferencia #{transfer.id} despachada. Quedó en tránsito.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/transferencias/<int:transfer_id>/recibir", methods=["POST"])
@tenant_required
def receive_transfer(transfer_id):
    _require_admin()
    try:
        transfer = _get_transfer_or_404(transfer_id)
        if transfer.status != "en_transito":
            raise ValueError("La transferencia no está disponible para recepción.")
        target_session = (
            CashSession.query.filter_by(
                company_id=_company_id(),
                branch_id=transfer.target_branch_id,
                cash_register_id=transfer.target_register_id,
                status="abierta",
            )
            .with_for_update()
            .first()
        )
        if target_session is None:
            raise ValueError("Abrí la caja de destino antes de confirmar la recepción.")
        movement = CashMovement(
            session_id=target_session.id,
            user_id=current_user.id,
            company_id=_company_id(),
            branch_id=transfer.target_branch_id,
            cash_transfer_id=transfer.id,
            movement_type="ingreso",
            category="transferencia_recibida",
            amount=transfer.amount,
            description=f"Recepción de transferencia #{transfer.id} desde {transfer.source_branch.name}",
        )
        db.session.add(movement)
        transfer.status = "recibida"
        transfer.received_by_user_id = current_user.id
        transfer.received_at = utcnow()
        _record_session_event(
            target_session,
            "transferencia_recibida",
            note=f"Transferencia #{transfer.id} recibida por $ {transfer.amount}",
            summary=_session_summary(target_session),
            payload='{"transfer_id":%d,"direction":"in"}' % int(transfer.id),
        )
        _audit("cash_transfer_receive", "cash_transfer", transfer.id, "Transferencia recibida")
        db.session.commit()
        flash(f"Transferencia #{transfer.id} recibida correctamente.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/transferencias/<int:transfer_id>/rechazar", methods=["POST"])
@tenant_required
def reject_transfer(transfer_id):
    _require_admin()
    try:
        transfer = _get_transfer_or_404(transfer_id)
        if transfer.status != "en_transito":
            raise ValueError("Solo se puede rechazar una transferencia que esté en tránsito.")
        source_session = (
            CashSession.query.filter_by(
                company_id=_company_id(),
                branch_id=transfer.source_branch_id,
                cash_register_id=transfer.source_register_id,
                status="abierta",
            )
            .with_for_update()
            .first()
        )
        if source_session is None:
            raise ValueError("Para devolver el efectivo, primero debe estar abierta la caja de origen.")
        movement = CashMovement(
            session_id=source_session.id,
            user_id=current_user.id,
            company_id=_company_id(),
            branch_id=transfer.source_branch_id,
            cash_transfer_id=transfer.id,
            movement_type="ingreso",
            category="transferencia_rechazada_devuelta",
            amount=transfer.amount,
            description=f"Devolución de transferencia rechazada #{transfer.id}",
        )
        db.session.add(movement)
        transfer.status = "rechazada"
        transfer.rejected_by_user_id = current_user.id
        transfer.rejected_at = utcnow()
        _record_session_event(
            source_session,
            "transferencia_devuelta",
            note=f"Transferencia #{transfer.id} devuelta a origen",
            summary=_session_summary(source_session),
            payload='{"transfer_id":%d,"direction":"return"}' % int(transfer.id),
        )
        _audit("cash_transfer_reject", "cash_transfer", transfer.id, "Transferencia rechazada y devuelta a origen")
        db.session.commit()
        flash(f"Transferencia #{transfer.id} rechazada; el efectivo volvió a la caja de origen.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/transferencias/<int:transfer_id>/cancelar", methods=["POST"])
@tenant_required
def cancel_transfer(transfer_id):
    _require_admin()
    try:
        transfer = _get_transfer_or_404(transfer_id)
        if transfer.status != "pendiente":
            raise ValueError("Solo se puede cancelar una transferencia antes de despacharla.")
        transfer.status = "cancelada"
        _audit("cash_transfer_cancel", "cash_transfer", transfer.id, "Transferencia cancelada antes del despacho")
        db.session.commit()
        flash(f"Transferencia #{transfer.id} cancelada.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/cuentas/crear", methods=["POST"])
@tenant_required
def create_financial_account():
    _require_admin()
    try:
        name = (request.form.get("name") or "").strip()[:120]
        code = (request.form.get("code") or "").strip().upper()[:40]
        account_type = (request.form.get("account_type") or "banco").strip().lower()
        payment_method_key = (request.form.get("payment_method_key") or "").strip().lower() or None
        if len(name) < 2:
            raise ValueError("Ingresá un nombre válido para la cuenta.")
        if account_type not in {"banco", "billetera", "tarjeta", "otros"}:
            raise ValueError("Tipo de cuenta no válido.")
        if payment_method_key not in {None, "mercado_pago", "debito", "credito", "transferencia", "otros"}:
            raise ValueError("El método de pago asociado no es válido.")
        branch_id = request.form.get("branch_id", type=int)
        branch = _get_company_branch(branch_id, lock=True) if branch_id else None
        if not code:
            code = "CTA-" + uuid4().hex[:8].upper()
        if FinancialAccount.query.filter_by(
            company_id=_company_id(), branch_id=(branch.id if branch else None), code=code
        ).first():
            raise ValueError("Ya existe una cuenta con ese código.")
        opening = _money(request.form.get("opening_balance"))
        account = FinancialAccount(
            company_id=_company_id(),
            branch_id=branch.id if branch else None,
            name=name,
            code=code,
            account_type=account_type,
            payment_method_key=payment_method_key,
            provider=(request.form.get("provider") or "").strip()[:80] or None,
            currency="ARS",
            opening_balance=opening,
            alias=(request.form.get("alias") or "").strip()[:120] or None,
            account_reference=(request.form.get("account_reference") or "").strip()[:120] or None,
            active=True,
            created_by_user_id=current_user.id,
        )
        db.session.add(account)
        db.session.flush()
        _audit("financial_account_create", "financial_account", account.id, f"Cuenta {name} creada.")
        db.session.commit()
        flash("Cuenta financiera creada.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except IntegrityError:
        db.session.rollback()
        flash("El código de cuenta ya existe. Revisá las cuentas registradas.", "warning")
    return redirect(url_for("cash_treasury.index"))


@bp.route("/cuentas/<int:account_id>/movimientos", methods=["POST"])
@tenant_required
def create_financial_account_movement(account_id):
    _require_admin()
    try:
        account = (
            FinancialAccount.query.filter_by(id=account_id, company_id=_company_id(), active=True)
            .with_for_update()
            .first_or_404()
        )
        movement_type = (request.form.get("movement_type") or "").strip().lower()
        if movement_type not in {"ingreso", "egreso", "ajuste"}:
            raise ValueError("Tipo de movimiento financiero no válido.")
        amount = _money(request.form.get("amount"), allow_negative=(movement_type == "ajuste"))
        if amount == 0 or (movement_type != "ajuste" and amount < 0):
            raise ValueError("Ingresá un importe válido y distinto de cero.")
        idempotency_key = (request.form.get("idempotency_key") or "").strip()[:100] or uuid4().hex
        if FinancialAccountMovement.query.filter_by(
            company_id=_company_id(), idempotency_key=idempotency_key
        ).first():
            flash("Este movimiento ya estaba registrado.", "info")
            return redirect(url_for("cash_treasury.index"))
        row = FinancialAccountMovement(
            company_id=_company_id(),
            branch_id=account.branch_id,
            account_id=account.id,
            movement_type=movement_type,
            amount=amount,
            description=(request.form.get("description") or "").strip()[:255] or None,
            reference=(request.form.get("reference") or "").strip()[:120] or None,
            source_type="manual",
            idempotency_key=idempotency_key,
            created_by_user_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("financial_account_movement", "financial_account_movement", row.id, f"{movement_type} $ {amount}")
        db.session.commit()
        flash("Movimiento financiero registrado.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except IntegrityError:
        db.session.rollback()
        flash("El movimiento ya fue recibido o está siendo procesado. No se duplicó el asiento.", "warning")
    return redirect(url_for("cash_treasury.index"))
