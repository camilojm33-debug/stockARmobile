import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from services.subscription_service import SubscriptionService


BASE_NOW = datetime(2026, 8, 29, 12, 0, 0)


def _company():
    return SimpleNamespace(active=True, trial_ends_at=BASE_NOW + timedelta(days=10))


def _subscription(*, status="active", next_billing_date=None, manual=False, cancel_at_period_end=False):
    metadata = {"is_manual": True, "managed_by": "superadmin"} if manual else {}
    return SimpleNamespace(
        status=status,
        next_billing_date=next_billing_date,
        ends_at=next_billing_date,
        start_date=BASE_NOW - timedelta(days=10),
        starts_at=BASE_NOW - timedelta(days=10),
        trial_end=None,
        cancel_at_period_end=cancel_at_period_end,
        renewal_enabled=True,
        auto_renew=True,
        metadata_json=json.dumps(metadata),
    )


def test_cancel_at_period_end_keeps_active_access_until_due(monkeypatch):
    monkeypatch.setattr("app.utcnow", lambda: BASE_NOW)
    subscription = _subscription(next_billing_date=BASE_NOW + timedelta(hours=4))

    class FakeService:
        STATE_ACTIVE = SubscriptionService.STATE_ACTIVE
        STATE_TRIAL = SubscriptionService.STATE_TRIAL
        STATE_PENDING = SubscriptionService.STATE_PENDING
        STATE_PENDING_PAYMENT = SubscriptionService.STATE_PENDING_PAYMENT
        STATE_PENDING_CONFIRMATION = SubscriptionService.STATE_PENDING_CONFIRMATION
        STATE_CANCELLED = SubscriptionService.STATE_CANCELLED
        OPEN_STATUSES = SubscriptionService.OPEN_STATUSES
        TERMINAL_STATUSES = SubscriptionService.TERMINAL_STATUSES

    result = SubscriptionService.resolve_company_access_state(_company(), subscription=subscription, now=BASE_NOW)
    assert result["status"] == SubscriptionService.STATE_ACTIVE
    assert result["can_access"] is True


def test_cancelled_at_period_end_blocks_without_grace_after_due(monkeypatch):
    due = BASE_NOW - timedelta(minutes=1)
    monkeypatch.setattr("app.utcnow", lambda: BASE_NOW)
    subscription = _subscription(next_billing_date=due, cancel_at_period_end=True)

    result = SubscriptionService.resolve_company_access_state(_company(), subscription=subscription, now=BASE_NOW)
    assert result["status"] == SubscriptionService.STATE_EXPIRED
    assert result["can_access"] is False


def test_overdue_automatic_subscription_gets_five_day_grace(monkeypatch):
    due = BASE_NOW - timedelta(days=1)
    monkeypatch.setattr("app.utcnow", lambda: BASE_NOW)
    subscription = _subscription(next_billing_date=due, cancel_at_period_end=False)

    result = SubscriptionService.resolve_company_access_state(_company(), subscription=subscription, now=BASE_NOW)
    assert result["status"] == SubscriptionService.STATE_PENDING_PAYMENT
    assert result["can_access"] is True


def test_overdue_manual_subscription_blocks_on_configured_due_date(monkeypatch):
    due = BASE_NOW - timedelta(seconds=1)
    monkeypatch.setattr("app.utcnow", lambda: BASE_NOW)
    subscription = _subscription(next_billing_date=due, manual=True)

    result = SubscriptionService.resolve_company_access_state(_company(), subscription=subscription, now=BASE_NOW)
    assert result["status"] == SubscriptionService.STATE_EXPIRED
    assert result["can_access"] is False


def test_cancel_at_period_end_does_not_change_active_status(monkeypatch):
    subscription = _subscription(next_billing_date=BASE_NOW + timedelta(days=5))
    company = SimpleNamespace(id=1)

    monkeypatch.setattr(
        SubscriptionService,
        "_target_subscription_for_command",
        staticmethod(lambda company_id, subscription_id: subscription),
    )

    result = SubscriptionService._handle_cancel(
        object(),
        company=company,
        command=SubscriptionService.CancelSubscriptionCommand(
            company_id=1,
            subscription_id=7,
            cancel_at_period_end=True,
        ),
    )

    assert subscription.status == SubscriptionService.STATE_ACTIVE
    assert subscription.cancel_at_period_end is True
    assert subscription.renewal_enabled is False
    assert subscription.auto_renew is False
    assert result.status_after == SubscriptionService.STATE_ACTIVE
