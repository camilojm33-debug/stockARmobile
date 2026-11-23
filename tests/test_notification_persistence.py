from app import Company, NotificationEvent, User, db
from services.notification_service import record_notification_event


def _company(name):
    company = Company(name=name, active=True)
    db.session.add(company)
    db.session.flush()
    return company


def _admin(company, suffix):
    user = User(
        username=f"notification-admin-{suffix}",
        email=f"notification-admin-{suffix}@example.test",
        password_hash="test-hash",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_notification_event_writer_is_idempotent_and_persists_source_metadata(app):
    with app.app_context():
        company = _company("Empresa notificaciones idempotentes")
        admin = _admin(company, "idempotent")
        args = dict(
            event_key="payment-webhook-evt-qa-001",
            title="Pago rechazado",
            body="El pago no pudo acreditarse.",
            href="/admin/portal",
            category="payments",
            priority="high",
            company_id=company.id,
            recipient_user_ids=[admin.id],
            source_type="mercadopago",
            source_id="evt-qa-001",
        )
        assert record_notification_event(db.session, **args) == 1
        db.session.commit()
        assert record_notification_event(db.session, **args) == 0
        db.session.commit()
        rows = NotificationEvent.query.filter_by(user_id=admin.id).all()
        assert len(rows) == 1
        assert rows[0].source_type == "mercadopago"
        assert rows[0].source_id == "evt-qa-001"
        assert rows[0].priority == "high"


def test_notification_history_and_read_actions_are_tenant_scoped(app):
    with app.app_context():
        first = _company("Empresa notificación A")
        second = _company("Empresa notificación B")
        first_admin = _admin(first, "tenant-a")
        second_admin = _admin(second, "tenant-b")
        first_event = NotificationEvent(
            user_id=first_admin.id,
            company_id=first.id,
            notification_key="tenant-a-event",
            category="system",
            priority="critical",
            title="Incidencia de respaldo A",
            body="Backup A falló.",
            href="/superadmin",
        )
        second_event = NotificationEvent(
            user_id=second_admin.id,
            company_id=second.id,
            notification_key="tenant-b-event",
            category="system",
            priority="critical",
            title="Incidencia de respaldo B",
            body="Backup B falló.",
            href="/superadmin",
        )
        db.session.add_all([first_event, second_event])
        db.session.commit()
        first_id = first_admin.id
        first_key = first_event.notification_key

    client = app.test_client()
    with app.app_context():
        first_admin = db.session.get(User, first_id)
        _login(client, first_admin)

    response = client.get("/api/notifications?view=all")
    assert response.status_code == 200
    payload = response.get_json()
    assert any(item["notification_key"] == first_key for item in payload["items"])
    assert not any(item["title"] == "Incidencia de respaldo B" for item in payload["items"])

    read_response = client.post("/api/notifications/mark-read", json={"notification_key": first_key})
    assert read_response.status_code == 200
    assert read_response.get_json()["ok"] is True

    resolve_response = client.post(f"/api/notifications/{first_key}/resolve", json={})
    assert resolve_response.status_code == 200
    with app.app_context():
        row = NotificationEvent.query.filter_by(user_id=first_id, notification_key=first_key).one()
        assert row.read_at is not None
        assert row.resolved_at is not None


def test_mark_all_read_only_updates_current_user_notifications(app):
    with app.app_context():
        company = _company("Empresa marcar todas")
        admin = _admin(company, "mark-all")
        other = _admin(company, "mark-all-other")
        db.session.add_all([
            NotificationEvent(user_id=admin.id, company_id=company.id, notification_key="all-a", category="general", priority="normal", title="A", body="A", href="/"),
            NotificationEvent(user_id=admin.id, company_id=company.id, notification_key="all-b", category="general", priority="normal", title="B", body="B", href="/"),
            NotificationEvent(user_id=other.id, company_id=company.id, notification_key="other-a", category="general", priority="normal", title="Otro", body="Otro", href="/"),
        ])
        db.session.commit()
        admin_id, other_id = admin.id, other.id

    client = app.test_client()
    with app.app_context():
        _login(client, db.session.get(User, admin_id))
    response = client.post("/api/notifications/mark-all-read", json={})
    assert response.status_code == 200
    with app.app_context():
        assert NotificationEvent.query.filter_by(user_id=admin_id, read_at=None).count() == 0
        assert NotificationEvent.query.filter_by(user_id=other_id, read_at=None).count() == 1
