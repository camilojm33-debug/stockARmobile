"""Servicio de suscripciones SaaS multiempresa."""

from __future__ import annotations

from datetime import timedelta


class SubscriptionService:
    TRIAL_STATUSES = {"trial", "trialing"}
    PENDING_STATUSES = {"pending", "pending_payment", "in_process", "authorized"}
    ACTIVE_STATUSES = {"active", "activa", "approved"}
    BLOCKED_STATUSES = {"suspended", "expired", "cancelled", "canceled", "rejected", "charged_back", "trial_expired"}

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
    def _trial_days() -> int:
        try:
            from services.plan_service import PlanService

            return int(getattr(PlanService, "TRIAL_DAYS", 10) or 10)
        except Exception:
            return 10

    @staticmethod
    def trial_end_for_company(company, now=None):
        from app import utcnow

        current = now or utcnow()
        trial_days = SubscriptionService._trial_days()
        return getattr(company, "trial_ends_at", None) or ((getattr(company, "created_at", None) or current) + timedelta(days=trial_days))

    @staticmethod
    def resolve_company_access_state(company, subscription=None, now=None):
        from app import utcnow

        current = now or utcnow()
        trial_end = SubscriptionService.trial_end_for_company(company, now=current)
        raw_status = ((getattr(subscription, "status", None) or "trial") if subscription is not None else "trial").lower()
        in_trial_window = bool(trial_end and current <= trial_end)

        if not getattr(company, "active", True):
            return {
                "status": "suspended",
                "subscription_status": raw_status,
                "can_access": False,
                "reason": "La empresa ha sido suspendida.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": getattr(subscription, "next_billing_date", None) if subscription is not None else None,
            }

        if subscription is None:
            if in_trial_window:
                return {
                    "status": "trial",
                    "subscription_status": "trial",
                    "can_access": True,
                    "reason": "Periodo de prueba activo.",
                    "trial_ends_at": trial_end,
                    "reference_date": trial_end,
                    "next_billing_date": trial_end,
                }
            return {
                "status": "trial_expired",
                "subscription_status": "trial_expired",
                "can_access": False,
                "reason": "Tu prueba expiró. Suscribite para continuar.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": trial_end,
            }

        if raw_status in SubscriptionService.TRIAL_STATUSES or raw_status in SubscriptionService.PENDING_STATUSES:
            if in_trial_window:
                return {
                    "status": "trial",
                    "subscription_status": raw_status,
                    "can_access": True,
                    "reason": "Periodo de prueba activo.",
                    "trial_ends_at": trial_end,
                    "reference_date": trial_end,
                    "next_billing_date": trial_end,
                }
            return {
                "status": "trial_expired",
                "subscription_status": raw_status,
                "can_access": False,
                "reason": "Tu prueba expiró. Suscribite para continuar.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": trial_end,
            }

        if raw_status in SubscriptionService.BLOCKED_STATUSES:
            return {
                "status": raw_status,
                "subscription_status": raw_status,
                "can_access": False,
                "reason": "La suscripción no está activa.",
                "trial_ends_at": trial_end,
                "reference_date": getattr(subscription, "next_billing_date", None) or getattr(subscription, "ends_at", None) or trial_end,
                "next_billing_date": getattr(subscription, "next_billing_date", None),
            }

        if raw_status in SubscriptionService.ACTIVE_STATUSES:
            paid_limit = getattr(subscription, "next_billing_date", None) or getattr(subscription, "ends_at", None)
            if paid_limit and current > paid_limit:
                return {
                    "status": "expired",
                    "subscription_status": raw_status,
                    "can_access": False,
                    "reason": "La suscripción está vencida.",
                    "trial_ends_at": trial_end,
                    "reference_date": paid_limit,
                    "next_billing_date": paid_limit,
                }
            return {
                "status": "active",
                "subscription_status": raw_status,
                "can_access": True,
                "reason": "Suscripción activa.",
                "trial_ends_at": trial_end,
                "reference_date": paid_limit,
                "next_billing_date": paid_limit,
            }

        # Fallback conservador: durante trial permitimos, fuera de trial bloqueamos.
        if in_trial_window:
            return {
                "status": "trial",
                "subscription_status": raw_status,
                "can_access": True,
                "reason": "Periodo de prueba activo.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": trial_end,
            }
        return {
            "status": "trial_expired",
            "subscription_status": raw_status,
            "can_access": False,
            "reason": "Tu prueba expiró. Suscribite para continuar.",
            "trial_ends_at": trial_end,
            "reference_date": trial_end,
            "next_billing_date": trial_end,
        }

    @staticmethod
    def sync_company_subscription_state(db_session, *, company, subscription=None, now=None):
        from app import utcnow

        current = now or utcnow()
        target_subscription = subscription or SubscriptionService.active_subscription_for_company(company.id)
        state = SubscriptionService.resolve_company_access_state(company, subscription=target_subscription, now=current)
        changed = False

        trial_end = state.get("trial_ends_at")
        if trial_end and getattr(company, "trial_ends_at", None) != trial_end:
            company.trial_ends_at = trial_end
            changed = True

        if target_subscription is not None:
            effective_status = state.get("status")
            raw_status = ((getattr(target_subscription, "status", None) or "trial")).lower()
            if effective_status == "trial" and raw_status != "trial":
                target_subscription.status = "trial"
                changed = True
            if effective_status == "trial_expired" and raw_status in (SubscriptionService.TRIAL_STATUSES | SubscriptionService.PENDING_STATUSES):
                target_subscription.status = "trial_expired"
                target_subscription.renewal_enabled = False
                target_subscription.auto_renew = False
                changed = True
            if effective_status in {"trial", "trial_expired"} and trial_end:
                if target_subscription.trial_end != trial_end:
                    target_subscription.trial_end = trial_end
                    changed = True
                if target_subscription.next_billing_date != trial_end:
                    target_subscription.next_billing_date = trial_end
                    changed = True
                if target_subscription.ends_at != trial_end:
                    target_subscription.ends_at = trial_end
                    changed = True

        state["changed"] = changed
        state["subscription"] = target_subscription
        if changed:
            db_session.flush()
        return state

    @staticmethod
    def ensure_company_trial(db_session, company, trial_plan):
        from app import Subscription, utcnow

        existing = SubscriptionService.active_subscription_for_company(company.id)
        if existing:
            return existing

        now = utcnow()
        trial_end = SubscriptionService.trial_end_for_company(company, now=now)
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

        trial_end = SubscriptionService.trial_end_for_company(company, now=now)
        company.trial_ends_at = trial_end

        subscription.plan_id = plan.id
        subscription.start_date = now
        subscription.starts_at = now

        if float(plan.price or 0) <= 0 or (plan.code or "").lower() == "trial":
            subscription.status = "trial"
            subscription.trial_end = trial_end
            subscription.ends_at = trial_end
            subscription.next_billing_date = trial_end
        else:
            has_successful_payment = bool(getattr(subscription, "last_payment_date", None))
            if has_successful_payment:
                subscription.status = "pending"
                subscription.trial_end = None
                subscription.ends_at = now + timedelta(days=int(plan.duration_days or 30))
                subscription.next_billing_date = subscription.ends_at
            else:
                in_trial_window = bool(trial_end and now <= trial_end)
                subscription.status = "trial" if in_trial_window else "trial_expired"
                subscription.trial_end = trial_end
                subscription.ends_at = trial_end
                subscription.next_billing_date = trial_end

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
