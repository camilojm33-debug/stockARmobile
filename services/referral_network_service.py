"""Multilevel referral network and SuperAdmin controls."""
from __future__ import annotations
from decimal import Decimal
from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import event
from app import db, superadmin_required

DEFAULT_PARENT_PERCENT = Decimal("0.3000")
MAX_PARENT_PERCENT = Decimal("0.5000")
network_bp = Blueprint("referral_network", __name__)

class ReferralNetworkLink(db.Model):
    __tablename__ = "referral_network_links"
    id = db.Column(db.Integer, primary_key=True)
    parent_seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    child_seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, unique=True, index=True)
    override_percent = db.Column(db.Numeric(6, 4), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), index=True)
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), onupdate=db.func.now())
    parent = db.relationship("ReferralSeller", foreign_keys=[parent_seller_id])
    child = db.relationship("ReferralSeller", foreign_keys=[child_seller_id])

class ReferralNetworkCommission(db.Model):
    __tablename__ = "referral_network_commissions"
    id = db.Column(db.Integer, primary_key=True)
    parent_seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    child_seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    source_commission_id = db.Column(db.Integer, db.ForeignKey("referral_commissions.id"), nullable=False, unique=True, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"), index=True)
    sold_amount = db.Column(db.Numeric(12, 2), nullable=False)
    commission_percent = db.Column(db.Numeric(6, 4), nullable=False, server_default="0.3000")
    commission_amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pendiente", index=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), index=True)
    paid_at = db.Column(db.DateTime)
    parent = db.relationship("ReferralSeller", foreign_keys=[parent_seller_id])
    child = db.relationship("ReferralSeller", foreign_keys=[child_seller_id])
    source_commission = db.relationship("ReferralCommission", foreign_keys=[source_commission_id])

def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))

def _percent(value):
    result = Decimal(str(value or DEFAULT_PARENT_PERCENT))
    return result if result in (Decimal("0.3000"), MAX_PARENT_PERCENT) else DEFAULT_PARENT_PERCENT

def validate_parent_assignment(*, parent_id, child_id):
    from app import ReferralSeller
    if parent_id == child_id:
        raise ValueError("Un vendedor no puede ser su propio padre.")
    parent = db.session.get(ReferralSeller, parent_id)
    child = db.session.get(ReferralSeller, child_id)
    if parent is None or child is None:
        raise ValueError("El vendedor seleccionado no existe.")
    if not parent.active:
        raise ValueError("El vendedor padre debe estar activo.")

def set_parent(*, child_id, parent_id, override_percent=None):
    validate_parent_assignment(parent_id=parent_id, child_id=child_id)
    current = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=parent_id, active=True).first()
    if current is not None and current.parent_seller_id == child_id:
        raise ValueError("La relación generaría un ciclo de referidos.")
    link = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=child_id).first()
    if link is None:
        link = ReferralNetworkLink(child_seller_id=child_id, parent_seller_id=parent_id)
        db.session.add(link)
    else:
        link.parent_seller_id = parent_id
        link.active = True
    link.override_percent = _percent(override_percent) if override_percent is not None else None
    db.session.flush()
    return link

def remove_parent(*, child_id):
    link = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=child_id).first()
    if link is not None:
        link.active = False
    return link

def parent_percent(link):
    return _percent(link.override_percent if link.override_percent is not None else DEFAULT_PARENT_PERCENT)

def install_commission_hook():
    from app import ReferralCommission, ReferralSeller
    if getattr(ReferralCommission, "_multilevel_hook_installed", False):
        return
    @event.listens_for(ReferralCommission, "after_insert")
    def _after_referral_commission(mapper, connection, target):
        link = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=target.seller_id, active=True).first()
        if link is None:
            return
        parent = db.session.get(ReferralSeller, link.parent_seller_id)
        child = db.session.get(ReferralSeller, target.seller_id)
        if parent is None or child is None or not parent.active or not child.active:
            return
        if db.session.query(ReferralNetworkCommission).filter_by(source_commission_id=target.id).first() is not None:
            return
        percent = parent_percent(link)
        sold_amount = _money(target.sold_amount)
        amount = (sold_amount * percent).quantize(Decimal("0.01"))
        if amount <= 0:
            return
        db.session.add(ReferralNetworkCommission(parent_seller_id=parent.id, child_seller_id=child.id, source_commission_id=target.id, company_id=target.company_id, payment_id=target.payment_id, subscription_id=target.subscription_id, sold_amount=sold_amount, commission_percent=percent, commission_amount=amount, status=target.status))
    ReferralCommission._multilevel_hook_installed = True

def network_snapshot():
    from app import ReferralSeller
    sellers = ReferralSeller.query.order_by(ReferralSeller.active.desc(), ReferralSeller.id.asc()).all()
    links = ReferralNetworkLink.query.filter_by(active=True).all()
    pending = sum((_money(x.commission_amount) for x in ReferralNetworkCommission.query.filter(ReferralNetworkCommission.status.in_(["pendiente", "disponible"])).all()), Decimal("0.00"))
    paid = sum((_money(x.commission_amount) for x in ReferralNetworkCommission.query.filter_by(status="pagada").all()), Decimal("0.00"))
    return {"sellers": sellers, "links": links, "pending": pending, "paid": paid, "total_network_commissions": ReferralNetworkCommission.query.count()}

@network_bp.route("/superadmin/referrals/network")
@superadmin_required
def dashboard():
    snap = network_snapshot()
    children = {row.parent_seller_id: [] for row in snap["links"]}
    for row in snap["links"]:
        children.setdefault(row.parent_seller_id, []).append(row.child)
    return render_template("saas/referrals_network.html", **snap, children=children)

@network_bp.route("/superadmin/referrals/network/link", methods=["POST"])
@superadmin_required
def link_seller():
    try:
        child_id = int(request.form.get("child_id"))
        parent_id = int(request.form.get("parent_id"))
        override = request.form.get("override_percent")
        override_value = Decimal(override) / Decimal("100") if override else None
        if override_value is not None and override_value not in (Decimal("0.30"), Decimal("0.50")):
            raise ValueError("La comisión de red solo puede ser 30% o 50%.")
        set_parent(child_id=child_id, parent_id=parent_id, override_percent=override_value)
        db.session.commit()
        flash("Relación padre → vendedor hijo guardada correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("referral_network.dashboard"))

@network_bp.route("/superadmin/referrals/network/<int:child_id>/unlink", methods=["POST"])
@superadmin_required
def unlink_seller(child_id):
    remove_parent(child_id=child_id)
    db.session.commit()
    flash("El vendedor quedó sin padre de red. El historial de comisiones se conserva.", "success")
    return redirect(url_for("referral_network.dashboard"))

@network_bp.route("/superadmin/referrals/network/<int:child_id>/percent", methods=["POST"])
@superadmin_required
def set_percent(child_id):
    try:
        value = Decimal(request.form.get("percent", "30")) / Decimal("100")
        if value not in (Decimal("0.30"), Decimal("0.50")):
            raise ValueError("Solo se permite 30% o 50%.")
        link = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=child_id, active=True).first()
        if link is None:
            raise ValueError("El vendedor no tiene un padre de red activo.")
        link.override_percent = None if value == DEFAULT_PARENT_PERCENT else value
        db.session.commit()
        flash(f"Comisión del padre actualizada a {int(value * 100)}%.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("referral_network.dashboard"))
