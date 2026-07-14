"""Servicio de suscripciones SaaS multiempresa."""

from __future__ import annotations

from datetime import timedelta


class SubscriptionService:
    PAYMENT_STATUS_MAP = {
        "approved": "active",
        "authorized": "pending",
        "pending": "pending",
        "in_process": "pending",
        "cancelled": "cancelled",
        "expired": "expired",
        "rejected": "rejected",
        "refunded": "active",
        "charged_back": "suspended",
    }

    @staticmethod
    def active_subscription_for_company(company_id: int):
        from app import Subscription

        return (
            Subscription.query.filter_by(company_id=company_id)
            .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
            .first()
        )

    @staticmethod
    def ensure_company_trial(db_session, company, trial_plan):
        from app import Subscription, utcnow

        existing = SubscriptionService.active_subscription_for_company(company.id)
        if existing:
            return existing

        now = utcnow()
        trial_end = company.trial_ends_at or (now + timedelta(days=10))
        company.trial_ends_at = trial_end
        subscription = Subscription(
            company_id=company.id,
            plan_id=trial_plan.id if trial_plan else None,
            status="trial",
            trial_end=trial_end,
            start_date=now,
            starts_at=now,
            ends_at=trial_end,
            next_billing_date=trial_end,
            renewal_enabled=True,
            auto_renew=True,
            cancel_at_period_end=False,
        )
        db_session.add(subscription)
        return subscription

    @staticmethod
    def start_or_change_plan(db_session, *, company, plan, user_id: int | None, external_reference: str | None = None):
        from app import Subscription, utcnow

        subscription = SubscriptionService.active_subscription_for_company(company.id)
        now = utcnow()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db_session.add(subscription)

        subscription.plan_id = plan.id
        subscription.status = "pending" if float(plan.price or 0) > 0 else "active"
        subscription.start_date = now
        subscription.starts_at = now
        subscription.ends_at = now + timedelta(days=int(plan.duration_days or 30))
        subscription.trial_end = now + timedelta(days=10) if plan.code == "trial" else None
        subscription.next_billing_date = subscription.ends_at
        subscription.cancel_at_period_end = False
        subscription.renewal_enabled = True
        subscription.auto_renew = True
        subscription.external_reference = external_reference
        db_session.add(subscription)
        return subscription

    @staticmethod
    def apply_payment_status(subscription, payment_status: str):
        from app import utcnow

        previous_status = (subscription.status or "pending").lower()
        normalized = (payment_status or "pending").lower()
        mapped = SubscriptionService.PAYMENT_STATUS_MAP.get(normalized, normalized)
        subscription.status = mapped

        if mapped in {"active", "approved"}:
            now = utcnow()
            subscription.last_payment_date = now
            duration = int(subscription.plan.duration_days if subscription.plan else 30)

            # First successful payment activates immediately.
            # Renewals only extend from next_billing_date if the current period is still open.
            if previous_status in {"active", "approved"} and subscription.next_billing_date and subscription.next_billing_date > now:
                period_start = subscription.next_billing_date
            else:
                period_start = now

            subscription.start_date = period_start
            subscription.starts_at = period_start
            subscription.next_billing_date = period_start + timedelta(days=duration)
            subscription.ends_at = subscription.next_billing_date
            subscription.renewal_enabled = True
            subscription.auto_renew = True
        elif mapped in {"cancelled", "expired", "suspended", "rejected"}:
            subscription.renewal_enabled = False
            subscription.auto_renew = False

        if subscription.cancel_at_period_end:
            subscription.renewal_enabled = False
            subscription.auto_renew = False
        return subscription
