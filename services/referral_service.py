"""Servicio centralizado para referidos, comisiones y pagos."""

from __future__ import annotations

import secrets
import string
from datetime import timedelta
from decimal import Decimal


class ReferralService:
    COMMISSION_PERCENT = Decimal("0.30")

    @staticmethod
    def _now():
        from app import utcnow

        return utcnow()

    @staticmethod
    def _base_url() -> str:
        import os

        app_url = (os.environ.get("APP_URL") or "https://www.stockarmobile.com").strip().rstrip("/")
        return app_url or "https://www.stockarmobile.com"

    @classmethod
    def generate_referral_code(cls, db_session) -> str:
        from app import ReferralSeller

        alphabet = string.ascii_uppercase + string.digits
        for _ in range(20):
            candidate = f"REF{''.join(secrets.choice(alphabet) for _ in range(6))}"
            if ReferralSeller.query.filter_by(referral_code=candidate).first() is None:
                return candidate

        # Fallback deterministico ante colisiones extremas.
        last = ReferralSeller.query.order_by(ReferralSeller.id.desc()).first()
        next_number = (last.id + 1) if last else 1
        return f"REF{next_number:06d}"

    @classmethod
    def build_referral_url(cls, referral_code: str) -> str:
        return f"{cls._base_url()}/?ref={referral_code}"

    @classmethod
    def create_or_update_seller(cls, db_session, *, user, profile_data: dict, profile=None):
        from app import ReferralSeller

        if profile is None:
            referral_code = cls.generate_referral_code(db_session)
            profile = ReferralSeller(
                user_id=user.id,
                referral_code=referral_code,
                referral_url=cls.build_referral_url(referral_code),
            )
            db_session.add(profile)

        for key in [
            "dni",
            "tax_id",
            "phone",
            "province",
            "city",
            "address",
            "alias",
            "cvu",
            "cbu",
            "bank",
            "account_holder",
            "active",
        ]:
            if key in profile_data:
                setattr(profile, key, profile_data[key])

        return profile

    @classmethod
    def find_seller_by_code(cls, code: str | None):
        from app import ReferralSeller

        normalized = (code or "").strip().upper()
        if not normalized:
            return None
        return ReferralSeller.query.filter_by(referral_code=normalized, active=True).first()

    @classmethod
    def attribute_company(cls, db_session, *, seller, company, user, referral_code: str):
        from app import ReferralAttribution

        # Anti-fraude: no permitir autorreferido directo por mismo usuario/email.
        if user is not None and getattr(seller, "user_id", None) == getattr(user, "id", None):
            return None
        seller_user = getattr(seller, "user", None)
        seller_email = (getattr(seller_user, "email", None) or "").strip().lower()
        user_email = (getattr(user, "email", None) or "").strip().lower() if user is not None else ""
        if seller_email and user_email and seller_email == user_email:
            return None

        existing = ReferralAttribution.query.filter_by(company_id=company.id).first()
        if existing:
            return existing

        row = ReferralAttribution(
            seller_id=seller.id,
            company_id=company.id,
            user_id=user.id if user else None,
            referral_code=(referral_code or seller.referral_code).strip().upper(),
        )
        db_session.add(row)
        return row

    @classmethod
    def create_commission_for_sale(cls, db_session, *, company_id: int, subscription=None, payment=None, plan=None):
        from app import ReferralAttribution, ReferralCommission

        attribution = ReferralAttribution.query.filter_by(company_id=company_id).first()
        if attribution is None:
            return None

        sold_amount = Decimal(str((payment.amount if payment is not None else (plan.price if plan is not None else 0)) or 0))
        if sold_amount <= 0:
            return None

        existing = None
        if payment is not None:
            existing = ReferralCommission.query.filter_by(payment_id=payment.id).first()
        if existing is not None:
            return existing

        commission_amount = (sold_amount * cls.COMMISSION_PERCENT).quantize(Decimal("0.01"))
        now = cls._now()
        commission = ReferralCommission(
            seller_id=attribution.seller_id,
            attribution_id=attribution.id,
            company_id=company_id,
            subscription_id=getattr(subscription, "id", None),
            payment_id=getattr(payment, "id", None),
            plan_id=getattr(plan, "id", None),
            sold_amount=sold_amount,
            commission_percent=Decimal("0.3000"),
            commission_amount=commission_amount,
            status="pendiente",
            created_at=now,
            available_at=now + timedelta(days=30),
        )
        db_session.add(commission)
        return commission

    @classmethod
    def refresh_commission_states(cls, db_session):
        from app import Company, ReferralCommission

        now = cls._now()
        rows = (
            ReferralCommission.query.filter(ReferralCommission.status.in_(["pendiente", "disponible"]))
            .order_by(ReferralCommission.created_at.asc())
            .all()
        )
        for row in rows:
            company = db_session.get(Company, row.company_id)
            if row.status == "pendiente":
                if company is not None and not company.active:
                    row.status = "anulada"
                    row.cancelled_at = now
                elif row.available_at and row.available_at <= now:
                    row.status = "disponible"
            elif row.status == "disponible":
                if company is not None and not company.active:
                    row.status = "anulada"
                    row.cancelled_at = now

    @classmethod
    def register_payout(
        cls,
        db_session,
        *,
        seller_id: int,
        commission_ids: list[int],
        processed_by_user_id: int,
        transfer_date,
        payment_method: str | None = None,
        receipt: str | None = None,
        transfer_number: str | None = None,
        observations: str | None = None,
    ):
        from app import ReferralCommission, ReferralPayout, ReferralPayoutItem

        commissions = (
            ReferralCommission.query.filter(
                ReferralCommission.id.in_(commission_ids),
                ReferralCommission.seller_id == seller_id,
                ReferralCommission.status == "disponible",
            )
            .all()
        )
        total = sum((Decimal(str(c.commission_amount or 0)) for c in commissions), Decimal("0.00"))
        payout = ReferralPayout(
            seller_id=seller_id,
            processed_by_user_id=processed_by_user_id,
            amount=total,
            transfer_date=transfer_date,
            payment_method=(payment_method or "").strip() or None,
            receipt=(receipt or "").strip() or None,
            transfer_number=(transfer_number or "").strip() or None,
            observations=(observations or "").strip() or None,
        )
        db_session.add(payout)
        db_session.flush()

        now = cls._now()
        for commission in commissions:
            commission.status = "pagada"
            commission.paid_at = now
            db_session.add(ReferralPayoutItem(payout_id=payout.id, commission_id=commission.id))

        return payout

    @classmethod
    def seller_dashboard_snapshot(cls, seller_id: int):
        from app import Company, ReferralAttribution, ReferralCommission, Subscription, db

        cls.refresh_commission_states(db.session)
        db.session.flush()

        attributions = ReferralAttribution.query.filter_by(seller_id=seller_id).all()
        company_ids = [row.company_id for row in attributions]

        active_clients = 0
        pending_clients = 0
        if company_ids:
            active_clients = Company.query.filter(Company.id.in_(company_ids), Company.active.is_(True)).count()
            pending_clients = Company.query.filter(Company.id.in_(company_ids), Company.active.is_(False)).count()

        commissions = ReferralCommission.query.filter_by(seller_id=seller_id).all()
        by_status = {"pendiente": Decimal("0.00"), "disponible": Decimal("0.00"), "pagada": Decimal("0.00"), "anulada": Decimal("0.00")}
        sold_total = Decimal("0.00")
        for row in commissions:
            sold_total += Decimal(str(row.sold_amount or 0))
            key = (row.status or "pendiente").lower()
            if key in by_status:
                by_status[key] += Decimal(str(row.commission_amount or 0))

        month_sales = (
            db.session.query(db.func.count(ReferralCommission.id))
            .filter(
                ReferralCommission.seller_id == seller_id,
                db.extract("year", ReferralCommission.created_at) == db.extract("year", db.func.current_timestamp()),
                db.extract("month", ReferralCommission.created_at) == db.extract("month", db.func.current_timestamp()),
            )
            .scalar()
            or 0
        )

        renewals = (
            db.session.query(db.func.count(Subscription.id))
            .join(ReferralAttribution, ReferralAttribution.company_id == Subscription.company_id)
            .filter(ReferralAttribution.seller_id == seller_id, Subscription.status.in_(["active", "approved", "trial"]))
            .scalar()
            or 0
        )

        total_clients = len(company_ids)
        conversion = round((active_clients / total_clients) * 100, 2) if total_clients else 0.0

        return {
            "total_clients": total_clients,
            "active_clients": active_clients,
            "pending_clients": pending_clients,
            "month_sales": int(month_sales),
            "renewals": int(renewals),
            "conversion": conversion,
            "sold_total": sold_total,
            "commissions_by_status": by_status,
            "commissions": commissions,
            "attributions": attributions,
        }
