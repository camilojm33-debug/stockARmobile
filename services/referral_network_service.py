"""Multilevel referral network rules."""
from __future__ import annotations
from decimal import Decimal
from sqlalchemy import event
from app import db
DEFAULT_PARENT_PERCENT = Decimal("0.3000")
MAX_PARENT_PERCENT = Decimal("0.5000")

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
    from app import ReferralNetworkLink
    validate_parent_assignment(parent_id=parent_id, child_id=child_id)
    current = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=parent_id).first()
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
    from app import ReferralNetworkLink
    link = db.session.query(ReferralNetworkLink).filter_by(child_seller_id=child_id).first()
    if link is not None:
        link.active = False
    return link

def parent_percent(link):
    return _percent(link.override_percent if link.override_percent is not None else DEFAULT_PARENT_PERCENT)

def install_commission_hook():
    from app import ReferralCommission, ReferralNetworkCommission, ReferralNetworkLink, ReferralSeller
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
        existing = db.session.query(ReferralNetworkCommission).filter_by(source_commission_id=target.id).first()
        if existing is not None:
            return
        percent = parent_percent(link)
        sold_amount = _money(target.sold_amount)
        amount = (sold_amount * percent).quantize(Decimal("0.01"))
        if amount <= 0:
            return
        db.session.add(ReferralNetworkCommission(parent_seller_id=parent.id, child_seller_id=child.id, source_commission_id=target.id, company_id=target.company_id, payment_id=target.payment_id, subscription_id=target.subscription_id, sold_amount=sold_amount, commission_percent=percent, commission_amount=amount, status=target.status))
    ReferralCommission._multilevel_hook_installed = True

def network_snapshot():
    from app import ReferralNetworkLink, ReferralNetworkCommission, ReferralSeller
    sellers = ReferralSeller.query.order_by(ReferralSeller.active.desc(), ReferralSeller.id.asc()).all()
    links = ReferralNetworkLink.query.filter_by(active=True).all()
    link_by_child = {row.child_seller_id: row for row in links}
    children_by_parent = {}
    for row in links:
        children_by_parent.setdefault(row.parent_seller_id, []).append(row.child_seller_id)
    pending = Decimal("0.00")
    paid = Decimal("0.00")
    for row in ReferralNetworkCommission.query.all():
        if row.status in {"pendiente", "disponible"}:
            pending += _money(row.commission_amount)
        elif row.status == "pagada":
            paid += _money(row.commission_amount)
    return {"sellers": sellers, "links": links, "link_by_child": link_by_child, "children_by_parent": children_by_parent, "pending": pending, "paid": paid, "total_network_commissions": ReferralNetworkCommission.query.count()}
