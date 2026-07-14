"""Modulo de programa de referidos: SuperAdmin y portal vendedor."""

from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import seller_required, superadmin_required
from services.referral_service import ReferralService

bp = Blueprint("referrals", __name__)


def _normalize_digits(value: str | None) -> str:
    raw = (value or "").strip()
    return "".join(ch for ch in raw if ch.isdigit())


def _parse_transfer_date(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


@bp.route("/superadmin/referrals")
@superadmin_required
def admin_referrals_dashboard():
    from app import Company, ReferralCommission, ReferralSeller, db

    ReferralService.refresh_commission_states(db.session)
    db.session.commit()

    sellers = ReferralSeller.query.order_by(ReferralSeller.created_at.desc()).all()
    commissions = ReferralCommission.query.order_by(ReferralCommission.created_at.desc()).all()

    total_sold = sum(float(row.sold_amount or 0) for row in commissions)
    total_paid = sum(float(row.commission_amount or 0) for row in commissions if row.status == "pagada")
    pending_count = sum(1 for row in commissions if row.status == "pendiente")
    paid_count = sum(1 for row in commissions if row.status == "pagada")

    best_seller = (
        db.session.query(ReferralSeller, db.func.coalesce(db.func.sum(ReferralCommission.sold_amount), 0).label("sold"))
        .outerjoin(ReferralCommission, ReferralCommission.seller_id == ReferralSeller.id)
        .group_by(ReferralSeller.id)
        .order_by(db.text("sold DESC"))
        .first()
    )

    ranking = (
        db.session.query(ReferralSeller, db.func.coalesce(db.func.sum(ReferralCommission.sold_amount), 0).label("sold"))
        .outerjoin(ReferralCommission, ReferralCommission.seller_id == ReferralSeller.id)
        .group_by(ReferralSeller.id)
        .order_by(db.text("sold DESC"))
        .limit(3)
        .all()
    )

    return render_template(
        "saas/referrals_dashboard.html",
        sellers=sellers,
        commissions=commissions,
        stats={
            "sellers_count": len(sellers),
            "pending_count": pending_count,
            "paid_count": paid_count,
            "total_sold": total_sold,
            "total_paid": total_paid,
            "best_seller": best_seller,
        },
        ranking=ranking,
    )


@bp.route("/superadmin/referrals/sellers", methods=["GET", "POST"])
@superadmin_required
def admin_referrals_sellers():
    from app import ReferralSeller, User, db

    if request.method == "POST":
        seller_id = request.form.get("seller_id", type=int)
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()

        if seller_id:
            profile = ReferralSeller.query.filter_by(id=seller_id).first_or_404()
            user = db.session.get(User, profile.user_id)
            if user is None:
                abort(404)
        else:
            if not username or not email:
                flash("Usuario y email son obligatorios.", "danger")
                return redirect(url_for("referrals.admin_referrals_sellers"))
            if User.query.filter_by(username=username).first() is not None:
                flash("El usuario ya existe.", "danger")
                return redirect(url_for("referrals.admin_referrals_sellers"))
            if User.query.filter_by(email=email).first() is not None:
                flash("El email ya existe.", "danger")
                return redirect(url_for("referrals.admin_referrals_sellers"))

            user = User(
                username=username,
                email=email,
                role="seller",
                active=True,
            )
            temp_password = (request.form.get("temp_password") or "seller123").strip()
            user.set_password(temp_password)
            db.session.add(user)
            db.session.flush()
            profile = None

        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        user.first_name = first_name[:80] or None
        user.last_name = last_name[:80] or None
        user.email = email or user.email
        user.active = (request.form.get("active") or "1") == "1"
        user.role = "seller"

        cbu = _normalize_digits(request.form.get("cbu"))
        if cbu and len(cbu) != 22:
            flash("El CBU debe tener 22 digitos.", "danger")
            return redirect(url_for("referrals.admin_referrals_sellers"))

        profile_data = {
            "dni": (request.form.get("dni") or "").strip(),
            "tax_id": (request.form.get("tax_id") or "").strip() or None,
            "phone": (request.form.get("phone") or "").strip() or None,
            "province": (request.form.get("province") or "").strip() or None,
            "city": (request.form.get("city") or "").strip() or None,
            "address": (request.form.get("address") or "").strip() or None,
            "alias": (request.form.get("alias") or "").strip() or None,
            "cbu": cbu or None,
            "bank": (request.form.get("bank") or "").strip() or None,
            "account_holder": (request.form.get("account_holder") or "").strip() or None,
            "active": user.active,
        }

        if not profile_data["dni"]:
            flash("El DNI es obligatorio.", "danger")
            return redirect(url_for("referrals.admin_referrals_sellers"))

        profile = ReferralService.create_or_update_seller(db.session, user=user, profile_data=profile_data, profile=profile)
        db.session.commit()
        flash("Vendedor guardado correctamente.", "success")
        return redirect(url_for("referrals.admin_referrals_sellers"))

    sellers = ReferralSeller.query.order_by(ReferralSeller.created_at.desc()).all()
    return render_template("saas/referrals_sellers.html", sellers=sellers)


@bp.route("/superadmin/referrals/sellers/<int:seller_id>/toggle", methods=["POST"])
@superadmin_required
def admin_referrals_seller_toggle(seller_id):
    from app import ReferralSeller, User, db

    profile = ReferralSeller.query.filter_by(id=seller_id).first_or_404()
    user = db.session.get(User, profile.user_id)
    if user is None:
        abort(404)

    profile.active = not profile.active
    user.active = profile.active
    db.session.commit()
    flash("Estado del vendedor actualizado.", "success")
    return redirect(url_for("referrals.admin_referrals_sellers"))


@bp.route("/superadmin/referrals/commissions")
@superadmin_required
def admin_referrals_commissions():
    from app import ReferralCommission, db

    ReferralService.refresh_commission_states(db.session)
    db.session.commit()

    status = (request.args.get("status") or "all").strip().lower()
    query = ReferralCommission.query
    if status in {"pendiente", "disponible", "pagada", "anulada"}:
        query = query.filter(ReferralCommission.status == status)
    commissions = query.order_by(ReferralCommission.created_at.desc()).all()
    return render_template("saas/referrals_commissions.html", commissions=commissions, current_status=status)


@bp.route("/superadmin/referrals/payout", methods=["POST"])
@superadmin_required
def admin_referrals_register_payout():
    from app import db

    seller_id = request.form.get("seller_id", type=int)
    commission_ids = request.form.getlist("commission_ids")
    parsed_ids = [int(item) for item in commission_ids if str(item).isdigit()]
    transfer_date = _parse_transfer_date(request.form.get("transfer_date"))
    if not seller_id or not parsed_ids or transfer_date is None:
        flash("Datos de pago incompletos.", "danger")
        return redirect(url_for("referrals.admin_referrals_commissions"))

    ReferralService.register_payout(
        db.session,
        seller_id=seller_id,
        commission_ids=parsed_ids,
        processed_by_user_id=current_user.id,
        transfer_date=transfer_date,
        receipt=request.form.get("receipt"),
        transfer_number=request.form.get("transfer_number"),
        observations=request.form.get("observations"),
    )
    db.session.commit()
    flash("Pago registrado correctamente.", "success")
    return redirect(url_for("referrals.admin_referrals_commissions"))


@bp.route("/superadmin/referrals/export")
@superadmin_required
def admin_referrals_export():
    from app import ReferralCommission

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Seller", "Company", "Plan", "Sold", "Commission", "Status", "Created", "Available", "Paid"])
    for row in ReferralCommission.query.order_by(ReferralCommission.created_at.desc()).all():
        writer.writerow(
            [
                row.id,
                row.seller.user.username if row.seller and row.seller.user else "",
                row.company.name if row.company else "",
                row.plan.name if row.plan else "",
                f"{float(row.sold_amount or 0):.2f}",
                f"{float(row.commission_amount or 0):.2f}",
                row.status,
                row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
                row.available_at.strftime("%Y-%m-%d %H:%M") if row.available_at else "",
                row.paid_at.strftime("%Y-%m-%d %H:%M") if row.paid_at else "",
            ]
        )
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=referidos_comisiones.csv"
    return response


@bp.route("/referidos")
@seller_required
def seller_dashboard():
    from app import ReferralSeller, db

    profile = ReferralSeller.query.filter_by(user_id=current_user.id).first_or_404()
    snapshot = ReferralService.seller_dashboard_snapshot(profile.id)

    milestones = [1, 5, 10, 25, 50, 100]
    medals = [target for target in milestones if snapshot["total_clients"] >= target]
    return render_template("referrals/dashboard.html", profile=profile, snapshot=snapshot, medals=medals)


@bp.route("/referidos/clientes")
@seller_required
def seller_clients():
    from app import Company, ReferralAttribution, Subscription

    from app import ReferralSeller

    seller = ReferralSeller.query.filter_by(user_id=current_user.id).first_or_404()
    attributions = ReferralAttribution.query.filter_by(seller_id=seller.id).order_by(ReferralAttribution.created_at.desc()).all()
    rows = []
    for attr in attributions:
        company = Company.query.filter_by(id=attr.company_id).first()
        subscription = Subscription.query.filter_by(company_id=attr.company_id).order_by(Subscription.id.desc()).first()
        rows.append({"company": company, "subscription": subscription, "attribution": attr})
    return render_template("referrals/clients.html", rows=rows, seller=seller)


@bp.route("/referidos/comisiones")
@seller_required
def seller_commissions():
    from app import ReferralCommission, ReferralSeller, db

    seller = ReferralSeller.query.filter_by(user_id=current_user.id).first_or_404()
    ReferralService.refresh_commission_states(db.session)
    db.session.commit()
    commissions = ReferralCommission.query.filter_by(seller_id=seller.id).order_by(ReferralCommission.created_at.desc()).all()
    return render_template("referrals/commissions.html", commissions=commissions, seller=seller)


@bp.route("/referidos/datos-cobro", methods=["GET", "POST"])
@seller_required
def seller_billing_data():
    from app import ReferralSeller, db

    seller = ReferralSeller.query.filter_by(user_id=current_user.id).first_or_404()
    if request.method == "POST":
        cbu = _normalize_digits(request.form.get("cbu"))
        if cbu and len(cbu) != 22:
            flash("El CBU debe tener 22 digitos.", "danger")
            return redirect(url_for("referrals.seller_billing_data"))

        seller.alias = (request.form.get("alias") or "").strip() or None
        seller.cbu = cbu or None
        seller.bank = (request.form.get("bank") or "").strip() or None
        seller.account_holder = (request.form.get("account_holder") or "").strip() or None
        seller.tax_id = (request.form.get("tax_id") or "").strip() or None
        db.session.commit()
        flash("Datos de cobro actualizados.", "success")
        return redirect(url_for("referrals.seller_billing_data"))

    missing_bank_data = not (seller.alias and seller.cbu and seller.bank and seller.account_holder)
    return render_template("referrals/billing_data.html", seller=seller, missing_bank_data=missing_bank_data)
