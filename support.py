"""Modulo de soporte: creacion de tickets por usuarios y gestion SuperAdmin."""

import secrets
import string

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app import superadmin_required, tenant_required, utcnow

bp = Blueprint("support", __name__)

SUPPORT_REASONS = [
    "Olvide mi contrasena",
    "No puedo ingresar",
    "Problemas con suscripcion",
    "Problemas con productos",
    "Problemas con ventas",
    "Problemas con impresion",
    "Otra consulta",
]


def _normalize_reason(raw_value: str | None) -> str:
    candidate = (raw_value or "").strip()
    return candidate if candidate in SUPPORT_REASONS else "Otra consulta"


def _temporary_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@bp.route("/nuevo", methods=["GET", "POST"])
@login_required
def new_ticket():
    from app import SupportTicket, db, record_audit

    if request.method == "POST":
        reason = _normalize_reason(request.form.get("reason"))
        description = (request.form.get("description") or "").strip()
        email = (request.form.get("email") or "").strip() or (current_user.email or "")

        if not description:
            flash("Debes ingresar una descripcion del problema.", "danger")
            return render_template("support/new.html", support_reasons=SUPPORT_REASONS)

        ticket = SupportTicket(
            company_id=getattr(current_user, "company_id", None),
            user_id=current_user.id,
            email=email,
            reason=reason,
            description=description,
            status="pendiente",
            created_at=utcnow(),
        )
        db.session.add(ticket)
        db.session.flush()
        record_audit(
            action="support_ticket_create",
            entity="support_ticket",
            entity_id=ticket.id,
            detail=f"Ticket soporte creado. motivo={reason}",
        )
        db.session.commit()
        flash("Solicitud de soporte enviada correctamente.", "success")
        if getattr(current_user, "role", None) == "superadmin":
            return redirect(url_for("support.admin_index"))
        return redirect(url_for("support.my_tickets"))

    return render_template("support/new.html", support_reasons=SUPPORT_REASONS)


@bp.route("/mis-tickets")
@tenant_required
def my_tickets():
    from app import SupportTicket

    tickets = (
        SupportTicket.query.filter(SupportTicket.user_id == current_user.id)
        .order_by(SupportTicket.created_at.desc())
        .all()
    )
    return render_template("support/my_tickets.html", tickets=tickets)


@bp.route("/admin")
@superadmin_required
def admin_index():
    from app import SupportTicket

    status = (request.args.get("status") or "all").strip().lower()
    query = SupportTicket.query
    if status in {"pendiente", "resuelto"}:
        query = query.filter(SupportTicket.status == status)

    tickets = query.order_by(SupportTicket.created_at.desc()).all()
    return render_template("saas/support.html", tickets=tickets, current_status=status)


@bp.route("/admin/<int:ticket_id>")
@superadmin_required
def admin_detail(ticket_id):
    from app import SupportTicket

    ticket = SupportTicket.query.filter_by(id=ticket_id).first_or_404()
    temp_password_key = f"support_temp_password_{ticket.id}"
    temp_password = session.pop(temp_password_key, None)
    return render_template("saas/support_detail.html", ticket=ticket, temp_password=temp_password)


@bp.route("/admin/<int:ticket_id>/resolve", methods=["POST"])
@superadmin_required
def admin_resolve(ticket_id):
    from app import SupportTicket, db, record_audit

    ticket = SupportTicket.query.filter_by(id=ticket_id).first_or_404()
    ticket.status = "resuelto"
    ticket.resolved_at = utcnow()
    ticket.resolved_by_user_id = current_user.id
    ticket.resolved_note = (request.form.get("resolved_note") or "").strip() or None
    record_audit(
        action="support_ticket_resolve",
        entity="support_ticket",
        entity_id=ticket.id,
        detail="Ticket marcado como resuelto",
    )
    db.session.commit()
    flash("Ticket marcado como resuelto.", "success")
    return redirect(url_for("support.admin_detail", ticket_id=ticket.id))


@bp.route("/admin/<int:ticket_id>/temp-password", methods=["POST"])
@superadmin_required
def admin_generate_temp_password(ticket_id):
    from app import SupportTicket, User, db, record_audit

    ticket = SupportTicket.query.filter_by(id=ticket_id).first_or_404()
    user = User.query.filter_by(id=ticket.user_id).first()
    if user is None:
        abort(404)

    temporary_password = _temporary_password()
    user.set_password(temporary_password)
    require_change = (request.form.get("require_password_change") or "0") == "1"
    user.must_change_password = require_change
    ticket.resolved_note = (ticket.resolved_note or "")

    record_audit(
        action="support_temp_password_generate",
        entity="user",
        entity_id=user.id,
        detail=f"Password temporal generada desde ticket {ticket.id}",
    )
    db.session.commit()

    # Mostrar una sola vez: se extrae y limpia en la siguiente carga del detalle.
    session[f"support_temp_password_{ticket.id}"] = temporary_password
    flash("Contrasena temporal generada. Copiala ahora, se mostrara una sola vez.", "warning")
    return redirect(url_for("support.admin_detail", ticket_id=ticket.id))