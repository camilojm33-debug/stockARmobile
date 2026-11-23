from flask_login import login_user

from app import User, db
from services import notification_service


def _sample_items():
    return [
        {
            "type": "success",
            "title": "Ventas de hoy",
            "body": "Hay 4 ventas registradas.",
            "href": "/ventas/",
            "permission": "sales",
        },
        {
            "type": "warning",
            "title": "Stock bajo",
            "body": "3 productos llegaron al mínimo.",
            "href": "/productos/",
            "permission": "inventory",
        },
        {
            "type": "danger",
            "title": "Backups",
            "body": "Hay un respaldo con error.",
            "href": "/superadmin",
        },
    ]


def test_notification_items_include_priority_category_and_action_metadata():
    prepared = notification_service.prepare_notification_items(
        _sample_items(),
        role="superadmin",
    )

    assert [item["title"] for item in prepared] == [
        "Backups",
        "Stock bajo",
        "Ventas de hoy",
    ]
    assert prepared[0]["severity"] == "critical"
    assert prepared[0]["category"] == "system"
    assert prepared[0]["is_urgent"] is True
    assert prepared[1]["severity"] == "high"
    assert prepared[1]["category_label"] == "Inventario"
    assert prepared[2]["category"] == "sales"
    assert prepared[2]["action_label"] == "Revisar"


def test_employee_notification_feed_remains_scoped_to_granted_permissions(app):
    with app.app_context():
        employee = User(
            username="notification_employee",
            email="notification_employee@example.test",
            password_hash="unused-test-hash",
            role="user",
            permissions_json='["inventory"]',
            active=True,
        )
        db.session.add(employee)
        db.session.commit()

        with app.test_request_context("/"):
            login_user(employee)
            visible = notification_service.filter_notifications_for_user(
                notification_service.prepare_notification_items(
                    _sample_items(),
                    role="user",
                )
            )

            assert [item["title"] for item in visible] == ["Stock bajo"]


def test_individual_and_bulk_notification_reads_persist(app, monkeypatch):
    with app.app_context():
        superadmin = User(
            username="notification_superadmin",
            email="notification_superadmin@example.test",
            password_hash="unused-test-hash",
            role="superadmin",
            active=True,
        )
        db.session.add(superadmin)
        db.session.commit()

        prepared = notification_service.prepare_notification_items(
            _sample_items(),
            role="superadmin",
        )
        monkeypatch.setattr(notification_service, "build_notifications", lambda: prepared)

        with app.test_request_context("/"):
            login_user(superadmin)

            first_payload = notification_service.get_notification_payload()
            assert first_payload["count"] == 3
            assert first_payload["urgent_count"] == 2

            first_key = first_payload["items"][0]["notification_key"]
            read_result = notification_service.mark_notification_read(first_key)
            assert read_result["ok"] is True
            assert read_result["count"] == 2

            second_payload = notification_service.get_notification_payload()
            assert second_payload["count"] == 2
            assert second_payload["urgent_count"] == 1

            all_read_result = notification_service.mark_notifications_seen()
            assert all_read_result["ok"] is True
            final_payload = notification_service.get_notification_payload()
            assert final_payload["items"] == []
            assert final_payload["count"] == 0
            assert final_payload["urgent_count"] == 0



def test_final_feed_normalizes_notifications_added_by_extensions(app, monkeypatch):
    with app.app_context():
        admin = User(
            username="notification_admin",
            email="notification_admin@example.test",
            password_hash="unused-test-hash",
            role="admin",
            active=True,
        )
        db.session.add(admin)
        db.session.commit()

        extension_items = [
            {
                "type": "warning",
                "title": "Pago a proveedor",
                "body": "Una obligación vence pronto.",
                "href": "/compras/proveedores/1/pagos/1",
            }
        ]
        # Simulate a module that wraps build_notifications() and appends an
        # item after the main notification facade has returned.
        monkeypatch.setattr(
            notification_service,
            "build_notifications",
            lambda: extension_items,
        )

        with app.test_request_context("/"):
            login_user(admin)
            payload = notification_service.get_notification_payload()

            assert payload["count"] == 1
            assert payload["urgent_count"] == 1
            assert payload["items"][0]["severity"] == "high"
            assert payload["items"][0]["category"] == "purchases"


def test_final_feed_rechecks_employee_permissions_after_extensions(app, monkeypatch):
    with app.app_context():
        employee = User(
            username="notification_extension_employee",
            email="notification_extension_employee@example.test",
            password_hash="unused-test-hash",
            role="user",
            permissions_json='["inventory"]',
            active=True,
        )
        db.session.add(employee)
        db.session.commit()

        extension_items = [
            {
                "type": "success",
                "title": "Ventas",
                "body": "Datos de ventas.",
                "href": "/ventas/",
                "permission": "sales",
            },
            {
                "type": "warning",
                "title": "Stock bajo",
                "body": "Revisar inventario.",
                "href": "/productos/",
                "permission": "inventory",
            },
            {
                "type": "danger",
                "title": "Administración",
                "body": "Solo administradores.",
                "href": "/superadmin",
            },
        ]
        monkeypatch.setattr(
            notification_service,
            "build_notifications",
            lambda: extension_items,
        )

        with app.test_request_context("/"):
            login_user(employee)
            payload = notification_service.get_notification_payload()

            assert [item["title"] for item in payload["items"]] == ["Stock bajo"]
