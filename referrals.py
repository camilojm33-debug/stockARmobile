"""Modulo de programa de referidos: SuperAdmin y portal vendedor."""

from __future__ import annotations

import csv
from decimal import Decimal
from datetime import datetime
from io import StringIO
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus
import zipfile

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

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


def _seller_state(seller) -> tuple[str, str]:
    if not getattr(seller, "active", False):
        return "Suspendido", "danger"
    has_billing_data = bool(seller.alias and seller.cbu and seller.bank and seller.account_holder)
    if not has_billing_data:
        return "Pendiente", "warning"
    return "Activo", "success"


def _level_progress(total_sales: int):
    levels = [
        {"name": "Bronce", "target": 0},
        {"name": "Plata", "target": 10},
        {"name": "Oro", "target": 20},
        {"name": "Platino", "target": 40},
        {"name": "Diamante", "target": 80},
    ]

    current = levels[0]
    next_level = None
    for index, level in enumerate(levels):
        if total_sales >= level["target"]:
            current = level
            next_level = levels[index + 1] if index + 1 < len(levels) else None

    if next_level is None:
        return {
            "levels": levels,
            "current": current,
            "next": None,
            "percent": 100,
            "progress_text": f"{total_sales} ventas registradas. Nivel maximo alcanzado.",
        }

    start = current["target"]
    end = next_level["target"]
    segment_total = max(1, end - start)
    segment_current = max(0, total_sales - start)
    percent = int(round((segment_current / segment_total) * 100))
    return {
        "levels": levels,
        "current": current,
        "next": next_level,
        "percent": max(0, min(100, percent)),
        "progress_text": f"{total_sales} / {next_level['target']} ventas para llegar a {next_level['name']}.",
    }


def _pdf_from_lines(title: str, lines: list[str], download_name: str):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 72
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(52, y, title)
    y -= 28
    pdf.setFont("Helvetica", 11)
    for line in lines:
        if y < 60:
            pdf.showPage()
            pdf.setFont("Helvetica", 11)
            y = height - 60
        pdf.drawString(52, y, line)
        y -= 18
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=download_name)


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
    from app import AuditLog, Company, ReferralAttribution, ReferralCommission, ReferralPayout, ReferralSeller, Subscription, db
    from flask import current_app

    profile = ReferralSeller.query.filter_by(user_id=current_user.id).first_or_404()
    snapshot = ReferralService.seller_dashboard_snapshot(profile.id)

    attributions = (
        ReferralAttribution.query.filter_by(seller_id=profile.id)
        .order_by(ReferralAttribution.created_at.desc(), ReferralAttribution.id.desc())
        .all()
    )
    company_ids = [row.company_id for row in attributions]

    subscriptions_by_company = {}
    if company_ids:
        subscriptions = (
            Subscription.query.filter(Subscription.company_id.in_(company_ids))
            .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
            .all()
        )
        for sub in subscriptions:
            if sub.company_id not in subscriptions_by_company:
                subscriptions_by_company[sub.company_id] = sub

    commissions_by_company = {}
    for row in snapshot["commissions"]:
        bucket = commissions_by_company.setdefault(
            row.company_id,
            {
                "commission_amount": Decimal("0.00"),
                "status": row.status,
            },
        )
        bucket["commission_amount"] += Decimal(str(row.commission_amount or 0))
        if row.created_at and row.created_at >= (row.created_at if bucket.get("last_at") is None else bucket["last_at"]):
            bucket["status"] = row.status
            bucket["last_at"] = row.created_at

    rows = []
    for attr in attributions:
        company = Company.query.filter_by(id=attr.company_id).first()
        subscription = subscriptions_by_company.get(attr.company_id)
        commission_data = commissions_by_company.get(attr.company_id, {})
        monthly_amount = float(getattr(getattr(subscription, "plan", None), "price", 0) or 0)
        commission_amount = float(commission_data.get("commission_amount", 0) or 0)
        rows.append(
            {
                "company": company,
                "subscription": subscription,
                "attribution": attr,
                "monthly_amount": monthly_amount,
                "commission_amount": commission_amount,
                "commission_status": commission_data.get("status") or "-",
            }
        )

    clicks = AuditLog.query.filter_by(action="referral_link_click", user_id=profile.user_id).count()
    registrations_obtained = len(attributions)
    companies_created = registrations_obtained
    free_trials = sum(1 for row in rows if row["subscription"] is not None and (row["subscription"].status or "").lower() == "trial")
    active_subscriptions = sum(1 for row in rows if row["subscription"] is not None and (row["subscription"].status or "").lower() in {"active", "approved", "trial"})
    cancelled_subscriptions = sum(1 for row in rows if row["subscription"] is not None and (row["subscription"].status or "").lower() in {"cancelled", "expired", "suspended", "rejected"})
    conversion = round((registrations_obtained / clicks) * 100, 2) if clicks else 0.0

    commissions_by_status = snapshot["commissions_by_status"]
    commissions_pending = float(commissions_by_status.get("pendiente", 0) or 0)
    commissions_available = float(commissions_by_status.get("disponible", 0) or 0)
    commissions_paid = float(commissions_by_status.get("pagada", 0) or 0)
    total_historical = commissions_pending + commissions_available + commissions_paid

    payouts = (
        ReferralPayout.query.filter_by(seller_id=profile.id)
        .order_by(ReferralPayout.transfer_date.desc(), ReferralPayout.id.desc())
        .limit(50)
        .all()
    )

    total_sales = len(snapshot["commissions"])
    level_progress = _level_progress(total_sales)
    avg_commission = (total_historical / total_sales) if total_sales else 0.0
    monthly_target = 10
    monthly_sales = int(snapshot["month_sales"])
    monthly_progress_percent = int(round((monthly_sales / monthly_target) * 100)) if monthly_target else 0
    monthly_progress_percent = max(0, min(100, monthly_progress_percent))
    monthly_remaining = max(0, monthly_target - monthly_sales)
    estimated_commission = avg_commission * monthly_target

    notifications = []
    latest_sales = sorted(snapshot["commissions"], key=lambda row: (row.created_at or datetime.min), reverse=True)[:3]
    for row in latest_sales:
        notifications.append(
            {
                "title": "Nueva venta referida",
                "detail": f"{row.company.name if row.company else 'Empresa'} genero ARS {float(row.sold_amount or 0):.2f}",
                "created_at": row.created_at,
                "badge": "primary",
            }
        )
    latest_paid = [row for row in snapshot["commissions"] if row.status == "pagada"]
    latest_paid.sort(key=lambda row: (row.paid_at or datetime.min), reverse=True)
    for row in latest_paid[:2]:
        notifications.append(
            {
                "title": "Pago de comision realizado",
                "detail": f"Se acredito ARS {float(row.commission_amount or 0):.2f} por {row.company.name if row.company else 'empresa'}.",
                "created_at": row.paid_at,
                "badge": "success",
            }
        )
    for row in sorted(snapshot["commissions"], key=lambda item: (item.cancelled_at or datetime.min), reverse=True):
        if row.status == "anulada":
            notifications.append(
                {
                    "title": "Cambio de estado",
                    "detail": f"Una comision fue anulada para {row.company.name if row.company else 'empresa'}.",
                    "created_at": row.cancelled_at,
                    "badge": "warning",
                }
            )
            break

    notifications.sort(key=lambda item: (item["created_at"] or datetime.min), reverse=True)
    notifications = notifications[:8]

    seller_state_label, seller_state_color = _seller_state(profile)

    wa_text = f"Hola! Te comparto StockArmobile para gestionar ventas, stock y clientes: {profile.referral_url}"
    share_links = {
        "whatsapp": f"https://wa.me/?text={quote_plus(wa_text)}",
        "facebook": f"https://www.facebook.com/sharer/sharer.php?u={quote_plus(profile.referral_url)}",
        "instagram": "https://www.instagram.com/",
        "linkedin": f"https://www.linkedin.com/sharing/share-offsite/?url={quote_plus(profile.referral_url)}",
    }

    copy_templates = {
        "whatsapp": f"Quiero recomendarte StockArmobile. Te dejo mi enlace: {profile.referral_url}",
        "facebook": f"Estoy recomendando StockArmobile para negocios. Mira de que se trata: {profile.referral_url}",
        "instagram": f"Si tienes negocio, prueba StockArmobile. Link: {profile.referral_url}",
        "email": f"Hola,\n\nTe recomiendo StockArmobile para gestionar tu negocio.\nPuedes ver mas detalles aqui: {profile.referral_url}\n\nSaludos.",
    }

    videos_dir = Path(current_app.static_folder) / "assets" / "videos"
    has_demo_30 = (videos_dir / "demo-30.mp4").exists()
    has_demo_60 = (videos_dir / "demo-60.mp4").exists()
    has_demo_90 = (videos_dir / "demo-90.mp4").exists()
    has_ref_tutorial = (videos_dir / "tutorial-referidos.mp4").exists()
    has_sales_tutorial = (videos_dir / "como-vender.mp4").exists()
    demo_fallback = (current_app.config.get("LANDING_DEMO_VIDEO_URL") or "").strip()

    videos = [
        {
            "id": "video-demo-30",
            "title": "Video Demo 30 segundos",
            "description": "Presentacion ultra rapida para captar interes en menos de un minuto.",
            "thumbnail": url_for("static", filename="assets/social/thumb-demo-30.svg"),
            "available": has_demo_30,
            "view_url": url_for("static", filename="assets/videos/demo-30.mp4") if has_demo_30 else "",
            "download_url": url_for("static", filename="assets/videos/demo-30.mp4") if has_demo_30 else "",
        },
        {
            "id": "video-demo-60",
            "title": "Video Demo 60 segundos",
            "description": "Recorrido breve de ventas, stock y reportes para compartir en WhatsApp.",
            "thumbnail": url_for("static", filename="assets/social/thumb-demo-60.svg"),
            "available": bool(has_demo_60 or demo_fallback),
            "view_url": url_for("static", filename="assets/videos/demo-60.mp4") if has_demo_60 else demo_fallback,
            "download_url": url_for("static", filename="assets/videos/demo-60.mp4") if has_demo_60 else "",
        },
        {
            "id": "video-demo-90",
            "title": "Video Demo 90 segundos",
            "description": "Demo completa para reuniones comerciales y cierres con negocios medianos.",
            "thumbnail": url_for("static", filename="assets/social/thumb-demo-90.svg"),
            "available": has_demo_90,
            "view_url": url_for("static", filename="assets/videos/demo-90.mp4") if has_demo_90 else "",
            "download_url": url_for("static", filename="assets/videos/demo-90.mp4") if has_demo_90 else "",
        },
        {
            "id": "video-tutorial-referidos",
            "title": "Tutorial del Programa de Referidos",
            "description": "Explica activacion, seguimiento de clientes, comisiones y cobros.",
            "thumbnail": url_for("static", filename="assets/social/thumb-referidos.svg"),
            "available": has_ref_tutorial,
            "view_url": url_for("static", filename="assets/videos/tutorial-referidos.mp4") if has_ref_tutorial else "",
            "download_url": url_for("static", filename="assets/videos/tutorial-referidos.mp4") if has_ref_tutorial else "",
        },
        {
            "id": "video-como-vender",
            "title": "Como vender StockArmobile",
            "description": "Guia audiovisual para detectar necesidades y cerrar ventas con confianza.",
            "thumbnail": url_for("static", filename="assets/social/thumb-como-vender.svg"),
            "available": has_sales_tutorial,
            "view_url": url_for("static", filename="assets/videos/como-vender.mp4") if has_sales_tutorial else "",
            "download_url": url_for("static", filename="assets/videos/como-vender.mp4") if has_sales_tutorial else "",
        },
    ]

    social_images = [
        {
            "platform": "Facebook",
            "title": "Post Facebook: Control total del negocio",
            "preview_url": url_for("static", filename="assets/social/facebook-post.svg"),
            "download_url": url_for("static", filename="assets/social/facebook-post.svg"),
            "suggested_text": f"Controla stock, ventas, caja y clientes en un solo lugar. Pruebalo gratis: {profile.referral_url}",
        },
        {
            "platform": "Instagram",
            "title": "Post Instagram: Vende desde el celular",
            "preview_url": url_for("static", filename="assets/social/instagram-post.svg"),
            "download_url": url_for("static", filename="assets/social/instagram-post.svg"),
            "suggested_text": f"Tu negocio en la palma de tu mano. Gestiona todo con StockArmobile: {profile.referral_url}",
        },
        {
            "platform": "Historias",
            "title": "Historia: Demo en 60 segundos",
            "preview_url": url_for("static", filename="assets/social/story-post.svg"),
            "download_url": url_for("static", filename="assets/social/story-post.svg"),
            "suggested_text": f"Mira como vender y controlar stock en segundos. Prueba gratis: {profile.referral_url}",
        },
        {
            "platform": "Estados de WhatsApp",
            "title": "Estado: Prueba gratis 10 dias",
            "preview_url": url_for("static", filename="assets/social/whatsapp-status.svg"),
            "download_url": url_for("static", filename="assets/social/whatsapp-status.svg"),
            "suggested_text": f"Prueba StockArmobile gratis por 10 dias y ordena tu negocio hoy: {profile.referral_url}",
        },
        {
            "platform": "LinkedIn",
            "title": "Post LinkedIn: Profesionaliza tu operacion",
            "preview_url": url_for("static", filename="assets/social/linkedin-post.svg"),
            "download_url": url_for("static", filename="assets/social/linkedin-post.svg"),
            "suggested_text": f"Digitaliza procesos comerciales y mejora indicadores en tiempo real con StockArmobile: {profile.referral_url}",
        },
    ]

    whatsapp_messages = [
        f"Hola. Queria mostrarte StockArmobile. Es un sistema para controlar stock, ventas, clientes y caja desde cualquier dispositivo. Puedes probarlo gratis durante 10 dias. {profile.referral_url}",
        f"Todavia llevas el stock en una planilla? Con StockArmobile puedes controlar todo desde el celular y evitar quiebres de stock. Pruebalo gratis aqui: {profile.referral_url}",
        f"Si quieres vender mas rapido y con menos errores, StockArmobile te permite cobrar, descontar stock y registrar clientes en segundos. Mira aqui: {profile.referral_url}",
        f"Muchos negocios pierden dinero por no medir costos y caja al dia. StockArmobile te muestra reportes claros para decidir mejor. Prueba gratis: {profile.referral_url}",
        f"Si tienes equipo, con StockArmobile puedes asignar usuarios con permisos y mantener control de operaciones sin complicarte. Link: {profile.referral_url}",
        f"Quieres saber que productos se venden mas y cuales estan frenando tu rotacion? StockArmobile te lo muestra en el dashboard. Empieza hoy: {profile.referral_url}",
        f"StockArmobile funciona en celular, tablet y PC sin instalaciones complejas. Ideal para comercio, distribucion y venta diaria. Prueba gratis: {profile.referral_url}",
        f"Con StockArmobile puedes registrar compras, gastos y ventas en un mismo sistema para tener numeros reales de tu negocio. Te paso el acceso: {profile.referral_url}",
        f"Si hoy cobras en varios metodos de pago, StockArmobile te ayuda a ordenar caja y conciliacion diaria en minutos. Mira la demo aqui: {profile.referral_url}",
        f"Te comparto una herramienta que ayuda a vender mejor y controlar inventario sin perder tiempo. Se llama StockArmobile y tiene prueba gratis: {profile.referral_url}",
    ]

    email_templates = [
        {
            "name": "Primer contacto",
            "subject": "Te presento StockArmobile para ordenar tu negocio",
            "body": (
                "Hola,\n\n"
                "Vi que gestionas ventas y stock en tu negocio, por eso queria mostrarte StockArmobile."
                " Te permite controlar ventas, inventario, clientes y caja desde cualquier dispositivo.\n\n"
                f"Puedes empezar con prueba gratis aqui: {profile.referral_url}\n\n"
                "Quedo atento para ayudarte con la implementacion."
            ),
        },
        {
            "name": "Seguimiento",
            "subject": "Te comparto una demo corta de StockArmobile",
            "body": (
                "Hola,\n\n"
                "Retomo nuestro contacto para compartirte una demo breve de como funciona StockArmobile en el dia a dia."
                " Veras ventas, control de stock y reportes en pocos minutos.\n\n"
                f"Acceso a demo y prueba: {profile.referral_url}\n\n"
                "Si quieres, coordinamos una llamada de 15 minutos."
            ),
        },
        {
            "name": "Recordatorio",
            "subject": "Recordatorio: prueba gratis de StockArmobile",
            "body": (
                "Hola,\n\n"
                "Te recuerdo que puedes activar tu prueba gratis de StockArmobile y empezar hoy mismo"
                " a controlar stock, ventas y caja en un solo lugar.\n\n"
                f"Activalo aqui: {profile.referral_url}\n\n"
                "Si necesitas ayuda, te acompano en la configuracion inicial."
            ),
        },
        {
            "name": "Cierre de venta",
            "subject": "Listos para empezar con StockArmobile",
            "body": (
                "Hola,\n\n"
                "Segun lo que conversamos, StockArmobile cubre tus necesidades de control de inventario,"
                " ventas y reportes. La mejor forma de avanzar es activar la prueba y validar resultados"
                " reales en tu operacion.\n\n"
                f"Inicio inmediato: {profile.referral_url}\n\n"
                "Estoy disponible para acompanarte en el arranque."
            ),
        },
        {
            "name": "Cliente interesado",
            "subject": "Siguiente paso para implementar StockArmobile",
            "body": (
                "Hola,\n\n"
                "Excelente que te haya interesado StockArmobile. El siguiente paso es activar la prueba gratuita"
                " y cargar tus productos principales para que veas impacto desde el primer dia.\n\n"
                f"Comienza aqui: {profile.referral_url}\n\n"
                "Cuando quieras, te envio una guia rapida de puesta en marcha."
            ),
        },
    ]

    faq_items = [
        {"q": "Funciona desde celular?", "a": "Si, puedes operar desde celular, tablet o PC con la misma cuenta."},
        {"q": "Necesito instalar algo?", "a": "No, se usa desde navegador web sin instalaciones complejas."},
        {"q": "Tiene prueba gratis?", "a": "Si, incluye prueba gratuita para validar flujo real de trabajo."},
        {"q": "Cuantos usuarios puedo crear?", "a": "Depende del plan contratado y del nivel de operacion."},
        {"q": "Tiene soporte?", "a": "Si, hay soporte y canal de ayuda para resolver dudas operativas."},
        {"q": "Puedo controlar varias cajas?", "a": "Si, puedes registrar movimientos y cierres para control de caja."},
        {"q": "Permite registrar clientes frecuentes?", "a": "Si, puedes almacenar clientes e historial de compras."},
        {"q": "Sirve para negocios pequenos?", "a": "Si, esta pensado para emprendedores y comercios en crecimiento."},
        {"q": "Sirve para negocios con alto volumen?", "a": "Si, permite escalar procesos y orden operativo."},
        {"q": "Puedo cargar compras y gastos?", "a": "Si, integra compras, gastos y ventas en un mismo entorno."},
        {"q": "Se pueden ver reportes?", "a": "Si, cuenta con reportes de ventas, stock y rendimiento comercial."},
        {"q": "Se puede usar para control de stock por producto?", "a": "Si, el stock se actualiza segun operaciones registradas."},
        {"q": "Tiene gestion de codigos de barras?", "a": "Si, incluye herramientas de etiquetas y codigos para productos."},
        {"q": "Puedo operar si no soy tecnico?", "a": "Si, la interfaz esta orientada a uso diario y rapido aprendizaje."},
        {"q": "Puedo exportar informacion?", "a": "Si, existen opciones de exportacion para analisis y gestion."},
        {"q": "Que pasa si mi internet falla?", "a": "Se recomienda conexion estable; tambien hay mejoras de experiencia offline en el sistema."},
        {"q": "Se adapta a distintos rubros?", "a": "Si, aplica a retail, distribucion y servicios con manejo de stock."},
        {"q": "Tiene seguridad de acceso?", "a": "Si, incluye autenticacion y controles de acceso por usuario."},
        {"q": "Puedo empezar rapido?", "a": "Si, puedes cargar productos base y vender el mismo dia."},
        {"q": "Como contrato luego de la prueba?", "a": "Desde el portal se selecciona plan y se activa suscripcion."},
    ]

    sales_guide_steps = [
        "Paso 1: Pregunta como controla hoy su stock, ventas y caja.",
        "Paso 2: Identifica una perdida concreta de tiempo o dinero en su proceso actual.",
        "Paso 3: Muestra la demo de 60 segundos enfocada en esa necesidad.",
        "Paso 4: Explica la prueba gratis y propone una meta medible para los primeros 7 dias.",
        "Paso 5: Cierra con una accion clara: activar prueba hoy y agendar seguimiento en 48 horas.",
    ]

    objection_handling = [
        {
            "objection": "No tengo tiempo.",
            "response": "Justamente por falta de tiempo conviene empezar. En una hora de configuracion inicial puedes ahorrar varias horas por semana en control manual.",
        },
        {
            "objection": "Ya uso otro sistema.",
            "response": "Perfecto. Podemos comparar tiempos de carga, claridad de reportes y facilidad de uso para validar si StockArmobile mejora tu operacion diaria.",
        },
        {
            "objection": "Es caro.",
            "response": "Cuando se mide stock perdido, errores de caja y tiempo operativo, el costo del sistema suele ser menor que el costo de no tener control.",
        },
        {
            "objection": "No entiendo de computadoras.",
            "response": "No necesitas conocimientos tecnicos. El flujo es simple y te acompano con una guia practica para tus primeras ventas y movimientos.",
        },
    ]

    sales_tips = [
        "No empieces por precio: empieza por problema de negocio.",
        "Habla con datos concretos de tiempo ahorrado por dia.",
        "Muestra una venta real en menos de un minuto.",
        "Conecta cada funcion con un beneficio operativo.",
        "Prioriza el dolor principal del cliente en la demo.",
        "Propone metas de prueba de 7 dias, no promesas vagas.",
        "Usa preguntas de diagnostico antes de presentar.",
        "Menciona resultados rapidos: orden de caja y stock.",
        "Comparte testimonios o casos similares de rubro.",
        "Resume al final tres beneficios y un siguiente paso.",
        "Agenda seguimiento antes de cerrar la llamada.",
        "Haz que el cliente navegue la demo contigo.",
        "Presenta la prueba gratis como decision sin riesgo.",
        "Enfoca el cierre en implementacion inmediata.",
        "Evita tecnicismos; usa lenguaje de negocio.",
        "Valida objeciones y responde con ejemplos reales.",
        "Comparte material visual despues de cada reunion.",
        "Adapta el discurso segun tamano del negocio.",
        "Mide conversion por canal para optimizar esfuerzos.",
        "Construye confianza cumpliendo cada seguimiento prometido.",
    ]

    resource_center = {
        "videos": videos,
        "social_images": social_images,
        "pdfs": [
            {"title": "Folleto Comercial", "url": url_for("referrals.seller_material_brochure")},
            {"title": "Catalogo de funciones", "url": url_for("referrals.seller_material_catalog")},
            {"title": "Comparativa de planes", "url": url_for("referrals.seller_material_plan_comparison")},
            {"title": "Programa de Referidos", "url": url_for("referrals.seller_material_referrals_program")},
        ],
        "whatsapp_messages": whatsapp_messages,
        "email_templates": email_templates,
        "faq_items": faq_items,
        "sales_guide_steps": sales_guide_steps,
        "objection_handling": objection_handling,
        "sales_tips": sales_tips,
        "share": {
            "whatsapp": share_links["whatsapp"],
            "facebook": share_links["facebook"],
            "linkedin": share_links["linkedin"],
            "referral_url": profile.referral_url,
            "referral_code": profile.referral_code,
        },
        "support": {
            "email": current_app.config.get("SUPPORT_EMAIL", "stockarmobile@gmail.com"),
            "email_link": f"mailto:{current_app.config.get('SUPPORT_EMAIL', 'stockarmobile@gmail.com')}",
            "whatsapp_display": current_app.config.get("SUPPORT_WHATSAPP_DISPLAY", "+54 9 3624 22-8296"),
            "whatsapp": f"https://wa.me/{current_app.config.get('SUPPORT_WHATSAPP_NUMBER', '5493624228296')}?text={quote_plus('Hola equipo de StockArmobile, necesito soporte comercial para vendedores.')}",
            "manual_pdf": url_for("referrals.seller_material_brochure"),
            "tutorial_anchor": "#resource-center-videos",
            "faq_anchor": "#resource-center-faq",
        },
    }

    milestones = [1, 5, 10, 25, 50, 100]
    medals = [target for target in milestones if snapshot["total_clients"] >= target]
    return render_template(
        "referrals/dashboard.html",
        profile=profile,
        snapshot=snapshot,
        medals=medals,
        seller_state_label=seller_state_label,
        seller_state_color=seller_state_color,
        share_links=share_links,
        copy_templates=copy_templates,
        stats={
            "clicks": clicks,
            "registrations": registrations_obtained,
            "companies_created": companies_created,
            "free_trials": free_trials,
            "active_subscriptions": active_subscriptions,
            "cancelled_subscriptions": cancelled_subscriptions,
            "conversion": conversion,
        },
        commission_cards={
            "pending": commissions_pending,
            "available": commissions_available,
            "paid": commissions_paid,
            "total_historical": total_historical,
            "balance_available": commissions_available,
        },
        clients_rows=rows,
        payouts=payouts,
        level_progress=level_progress,
        monthly_goal={
            "sales": monthly_sales,
            "target": monthly_target,
            "percent": monthly_progress_percent,
            "remaining": monthly_remaining,
            "next_level": "Plata",
            "estimated_commission": float(estimated_commission),
        },
        notifications=notifications,
        resource_center=resource_center,
    )


@bp.route("/referidos/activar", methods=["GET", "POST"])
@login_required
def activate_seller():
    from app import ReferralSeller, db

    if getattr(current_user, "role", None) == "superadmin":
        flash("El Programa de Referidos no esta disponible para SuperAdmin.", "warning")
        return redirect(url_for("saas.index"))

    existing = ReferralSeller.query.filter_by(user_id=current_user.id).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            db.session.commit()
        flash("Tu Programa de Referidos ya esta activo.", "info")
        return redirect(url_for("referrals.seller_dashboard"))

    if request.method == "POST":
        dni = (request.form.get("dni") or "").strip() or f"AUTO-{current_user.id}"
        profile_data = {
            "dni": dni,
            "tax_id": None,
            "phone": None,
            "province": None,
            "city": None,
            "address": None,
            "alias": None,
            "cbu": None,
            "bank": None,
            "account_holder": None,
            "active": True,
        }
        ReferralService.create_or_update_seller(db.session, user=current_user, profile_data=profile_data)
        db.session.commit()
        flash("Programa de Referidos activado correctamente.", "success")
        return redirect(url_for("referrals.seller_dashboard"))

    return render_template("referrals/activate.html")


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
    from app import ReferralSeller, User, db

    seller = ReferralSeller.query.filter_by(user_id=current_user.id).first_or_404()
    if request.method == "POST":
        cbu = _normalize_digits(request.form.get("cbu"))
        if cbu and len(cbu) != 22:
            flash("El CBU debe tener 22 digitos.", "danger")
            return redirect(url_for("referrals.seller_billing_data"))

        billing_email = (request.form.get("billing_email") or "").strip().lower()
        if billing_email:
            existing_email = User.query.filter(User.email == billing_email, User.id != current_user.id).first()
            if existing_email is not None:
                flash("El email de cobro ya esta en uso por otro usuario.", "danger")
                return redirect(url_for("referrals.seller_billing_data"))
            current_user.email = billing_email

        full_name = (request.form.get("full_name") or "").strip()
        if full_name:
            parts = full_name.split(" ", 1)
            current_user.first_name = parts[0][:80]
            current_user.last_name = (parts[1] if len(parts) > 1 else "")[:80] or None

        seller.dni = (request.form.get("dni") or "").strip() or seller.dni
        seller.alias = (request.form.get("alias") or "").strip() or None
        seller.cbu = cbu or None
        seller.bank = (request.form.get("bank") or "").strip() or None
        seller.account_holder = (request.form.get("account_holder") or full_name or "").strip() or None
        seller.tax_id = (request.form.get("tax_id") or "").strip() or None
        seller.phone = (request.form.get("phone") or "").strip() or None
        db.session.commit()
        flash("Datos de cobro actualizados.", "success")
        return redirect(url_for("referrals.seller_billing_data"))

    missing_bank_data = not (seller.alias and seller.cbu and seller.bank and seller.account_holder)
    return render_template("referrals/billing_data.html", seller=seller, missing_bank_data=missing_bank_data)


@bp.route("/referidos/materiales/folleto.pdf")
@seller_required
def seller_material_brochure():
    lines = [
        "StockArmobile es una plataforma para ventas, stock, clientes y caja.",
        "Beneficios para tus clientes:",
        "- Operacion centralizada en una sola herramienta.",
        "- Control de usuarios y seguridad con PIN en Mi Empresa.",
        "- Reportes para tomar decisiones con datos reales.",
        "- Implementacion rapida, sin instalacion compleja.",
    ]
    return _pdf_from_lines("Folleto Comercial StockArmobile", lines, "stockarmobile_folleto.pdf")


@bp.route("/referidos/materiales/catalogo.pdf")
@seller_required
def seller_material_catalog():
    lines = [
        "Catalogo Comercial:",
        "- Prueba Gratis 10 dias.",
        "- Plan Emprendedor.",
        "- Plan Negocio.",
        "- Plan Premium.",
        "Cada plan escala en capacidad de usuarios, productos y clientes.",
        "El detalle actualizado se consulta en la Landing oficial.",
    ]
    return _pdf_from_lines("Catalogo de Planes StockArmobile", lines, "stockarmobile_catalogo.pdf")


@bp.route("/referidos/materiales/comparativa-planes.pdf")
@seller_required
def seller_material_plan_comparison():
    lines = [
        "Comparativa de Planes StockArmobile:",
        "- Emprendedor: ideal para comenzar con operaciones basicas de ventas y stock.",
        "- Negocio: recomendado para equipos con mayor volumen y seguimiento comercial.",
        "- Premium: pensado para crecimiento, control avanzado y analitica extendida.",
        "Criterios de comparacion:",
        "1) Cantidad de usuarios operativos.",
        "2) Escala de productos y clientes administrados.",
        "3) Nivel de reportes y control de gestion.",
        "4) Soporte y acompanamiento segun necesidad.",
        "Tip comercial: vende por impacto operativo, no por precio aislado.",
    ]
    return _pdf_from_lines("Comparativa de Planes", lines, "stockarmobile_comparativa_planes.pdf")


@bp.route("/referidos/materiales/programa-referidos.pdf")
@seller_required
def seller_material_referrals_program():
    lines = [
        "Programa de Referidos StockArmobile:",
        "1) Comparte tu enlace personalizado.",
        "2) El cliente activa prueba o plan desde tu enlace.",
        "3) El sistema registra atribucion automaticamente.",
        "4) Las comisiones se calculan segun reglas vigentes.",
        "5) Consulta estados en tu portal: pendiente, disponible, pagada.",
        "Buenas practicas:",
        "- Realiza seguimiento en 24 horas despues de cada demo.",
        "- Envia material visual segun el canal del prospecto.",
        "- Registra objeciones para responder con argumentos concretos.",
    ]
    return _pdf_from_lines("Programa de Referidos", lines, "stockarmobile_programa_referidos.pdf")


@bp.route("/referidos/materiales/imagenes.zip")
@seller_required
def seller_material_images_zip():
    from flask import current_app

    icons_dir = Path(current_app.static_folder) / "assets" / "icons"
    files = [icons_dir / "icon-192.png", icons_dir / "icon-512.png"]
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            if file_path.exists():
                archive.write(file_path, arcname=file_path.name)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name="stockarmobile_imagenes.zip")
