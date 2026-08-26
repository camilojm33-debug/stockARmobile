import json

from services.billing_service import BillingService


class FakeSubscription:
    company_id = 10
    id = 20
    metadata_json = json.dumps({"mercadopago_preapproval_id": "preapproval-123"})


def test_cancel_subscription_cancels_remote_before_local(monkeypatch):
    subscription = FakeSubscription()
    calls = []

    class FakeMP:
        def get_preapproval(self, preapproval_id):
            calls.append(("get", preapproval_id))
            return {"id": preapproval_id, "status": "authorized"}

        def cancel_preapproval(self, preapproval_id):
            calls.append(("cancel", preapproval_id))
            return {"id": preapproval_id, "status": "canceled"}

    class FakeSubscriptionService:
        @staticmethod
        def _metadata_dict(value):
            return json.loads(value.metadata_json)

        CancelSubscriptionCommand = staticmethod(lambda **kwargs: kwargs)

        @staticmethod
        def run_command(db_session, command):
            calls.append(("local", command))

        @staticmethod
        def _set_metadata(value, updates):
            value.metadata_json = json.dumps({**json.loads(value.metadata_json), **updates})

    monkeypatch.setattr("services.billing_service.MercadoPagoService", FakeMP)
    monkeypatch.setattr("services.billing_service.SubscriptionService", FakeSubscriptionService)
    monkeypatch.setattr(
        "services.billing_service.NotificationService.record_event",
        lambda *args, **kwargs: calls.append(("event", kwargs)),
    )

    BillingService.cancel_subscription(object(), subscription=subscription, user_id=99)

    assert [item[0] for item in calls[:2]] == ["get", "cancel"]
    assert any(item[0] == "local" for item in calls)
    assert json.loads(subscription.metadata_json)["mercadopago_cancellation_requested"] is True
