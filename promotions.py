"""Gestión de promociones comerciales para empresas."""
from __future__ import annotations
from datetime import datetime, time
from decimal import Decimal
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import Index
from app import Company, Product, db, record_audit, utcnow, scope_query_to_company
from services.ai_agent.usage_service import can_use_commercial_feature
from stockarmobile.tenant import get_current_company_id

bp = Blueprint("promotions", __name__, url_prefix="/promociones")

class Promotion(db.Model):
    __tablename__ = "promotions"
    __table_args__ = (
        Index("ix_promotions_company_active_dates", "company_id", "active", "starts_at", "ends_at"),
        Index("ix_promotions_company_product", "company_id", "product_id"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True, index=True)
    category = db.Column(db.String(100))
    name = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text)
    type = db.Column(db.String(30), nullable=False)
    min_quantity = db.Column(db.Numeric(18, 3))
    buy_quantity = db.Column(db.Numeric(18, 3))
    pay_quantity = db.Column(db.Numeric(18, 3))
    discount_percent = db.Column(db.Numeric(10, 4))
    discount_amount = db.Column(db.Numeric(18, 2))
    starts_at = db.Column(db.DateTime)
    ends_at = db.Column(db.DateTime)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="ACTIVA", index=True)
    priority = db.Column(db.Integer, nullable=False, default=100)
    stackable = db.Column(db.Boolean, nullable=False, default=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    company = db.relationship("Company")
    product = db.relationship("Product")
    created_by = db.relationship("User")

def _access():
    company_id = get_current_company_id(current_user)
    company = Company.query.filter_by(id=company_id, active=True).first() if company_id else None
    return company, can_use_commercial_feature(company, "promotions") if company else None

def _require_access():
    company, access = _access()
    if company is None:
        flash("No se encontró la empresa activa.", "danger")
        return None, redirect(url_for("dashboard.index"))
    if access is None or not access.allowed:
        flash(access.reason if access else "Las promociones requieren el plan Negocio o superior.", "warning")
        return None, redirect(url_for("company_billing.subscription_portal"))
    if current_user.role != "admin":
        flash("Solo el administrador puede gestionar promociones.", "warning")
        return None, redirect(url_for("dashboard.index"))
    return company, None

def _parse_datetime(value: str, end=False):
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = datetime.strptime(raw, "%Y-%m-%d")
    return datetime.combine(parsed.date(), time.max if end else time.min)

def _decimal(value, default=None):
    if value in (None, ""):
        return default
    return Decimal(str(value).replace(",", "."))

def _validate_payload(form):
    ptype = (form.get("type") or "").strip().lower()
    if ptype not in {"bogo", "percent_quantity", "fixed_quantity"}:
        raise ValueError("Tipo de promoción inválido.")
    product_id = form.get("product_id")
    category = (form.get("category") or "").strip()[:100] or None
    if not product_id and not category:
        raise ValueError("Seleccioná un producto o una categoría.")
    if ptype == "bogo":
        buy = _decimal(form.get("buy_quantity"))
        pay = _decimal(form.get("pay_quantity"))
        if buy is None or pay is None or buy <= 0 or pay < 0 or pay >= buy:
            raise ValueError("Indicá una regla válida: comprás X y pagás Y.")
    elif ptype == "percent_quantity":
        minimum = _decimal(form.get("min_quantity"))
        percent = _decimal(form.get("discount_percent"))
        if minimum is None or minimum <= 0 or percent is None or percent <= 0 or percent > 100:
            raise ValueError("Indicá una cantidad mínima y un porcentaje entre 0 y 100.")
    else:
        minimum = _decimal(form.get("min_quantity"))
        amount = _decimal(form.get("discount_amount"))
        if minimum is None or minimum <= 0 or amount is None or amount <= 0:
            raise ValueError("Indicá una cantidad mínima y un importe de descuento válido.")
    starts = _parse_datetime(form.get("starts_at"))
    ends = _parse_datetime(form.get("ends_at"), end=True)
    if starts and ends and ends < starts:
        raise ValueError("La fecha de fin no puede ser anterior al inicio.")
    return {
        "product_id": int(product_id) if product_id else None,
        "category": category,
        "name": (form.get("name") or "").strip()[:180],
        "description": (form.get("description") or "").strip()[:2000] or None,
        "type": ptype,
        "min_quantity": _decimal(form.get("min_quantity")),
        "buy_quantity": _decimal(form.get("buy_quantity")),
        "pay_quantity": _decimal(form.get("pay_quantity")),
        "discount_percent": _decimal(form.get("discount_percent")),
        "discount_amount": _decimal(form.get("discount_amount")),
        "starts_at": starts,
        "ends_at": ends,
        "active": form.get("active") in {"1", "true", "on", "yes"},
        "status": "ACTIVA" if form.get("active") in {"1", "true", "on", "yes"} else "BORRADOR",
        "priority": max(0, min(int(form.get("priority") or 100), 10000)),
        "stackable": False,
    }

@bp.route("/", methods=["GET"])
@login_required
def index():
    company, response = _require_access()
    if response:
        return response
    promotions = Promotion.query.filter_by(company_id=company.id).order_by(Promotion.active.desc(), Promotion.priority.desc(), Promotion.id.desc()).all()
    products = scope_query_to_company(Product.query.filter(Product.active.is_(True)), Product).order_by(Product.name.asc()).all()
    categories = [row[0] for row in scope_query_to_company(Product.query.with_entities(Product.category), Product).distinct().order_by(Product.category.asc()).all() if row[0]]
    return render_template("promociones/index.html", promotions=promotions, products=products, categories=categories)

@bp.route("/guardar", methods=["POST"])
@login_required
def save():
    company, response = _require_access()
    if response:
        return response
    try:
        payload = _validate_payload(request.form)
        if not payload["name"]:
            raise ValueError("Ingresá un nombre para la promoción.")
        if payload["product_id"]:
            product = scope_query_to_company(Product.query.filter_by(id=payload["product_id"], active=True), Product).first()
            if product is None:
                raise ValueError("El producto seleccionado no pertenece a tu empresa.")
        promotion_id = request.form.get("promotion_id")
        promotion = Promotion.query.filter_by(id=int(promotion_id), company_id=company.id).first() if promotion_id else None
        if promotion is None:
            promotion = Promotion(company_id=company.id, created_by_user_id=current_user.id)
            db.session.add(promotion)
        for key, value in payload.items():
            setattr(promotion, key, value)
        db.session.flush()
        record_audit(action="promotion_saved", entity="promotion", entity_id=promotion.id, company_id=company.id, user_id=current_user.id, detail=promotion.name)
        db.session.commit()
        flash("Promoción guardada correctamente.", "success")
    except (ValueError, TypeError, OverflowError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("promotions.index"))

@bp.route("/<int:promotion_id>/toggle", methods=["POST"])
@login_required
def toggle(promotion_id):
    company, response = _require_access()
    if response:
        return response
    promotion = Promotion.query.filter_by(id=promotion_id, company_id=company.id).first_or_404()
    promotion.active = not bool(promotion.active)
    promotion.status = "ACTIVA" if promotion.active else "BORRADOR"
    db.session.commit()
    record_audit(action="promotion_toggled", entity="promotion", entity_id=promotion.id, company_id=company.id, user_id=current_user.id, detail=f"active={promotion.active}")
    return redirect(url_for("promotions.index"))
