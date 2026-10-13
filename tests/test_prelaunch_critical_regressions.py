import json
from decimal import Decimal

from app import Plan, User, db, load_user
from services.backup_service import BackupService
from services.plan_service import PlanService
from stockarmobile.permissions import employee_endpoint_permission


def test_plan_bootstrap_preserves_existing_admin_configuration(app):
    with app.app_context():
        existing = Plan(
            code="business",
            name="Plan configurado por Superadmin",
            price=Decimal("43123.00"),
            currency="ARS",
            duration_days=45,
            max_users=17,
            max_products=4321,
            max_clients=7654,
            features_json="inventario,ventas,crm",
            state="paused",
            active=False,
        )
        db.session.add(existing)
        db.session.commit()

        PlanService.ensure_defaults(db.session)
        db.session.refresh(existing)

        assert existing.name == "Plan configurado por Superadmin"
        assert float(existing.price) == 43123.0
        assert existing.duration_days == 45
        assert existing.max_users == 17
        assert existing.max_products == 4321
        assert existing.max_clients == 7654
        assert existing.features_json == "inventario,ventas,crm"
        assert existing.state == "paused"
        assert existing.active is False

        existing_codes = {row.code for row in Plan.query.all()}
        assert {"trial", "entrepreneur", "business", "premium"} <= existing_codes


def test_deactivated_user_is_not_restored_from_an_existing_login_session(app):
    with app.app_context():
        user = User(
            username="revoked_employee",
            email="revoked_employee@example.test",
            password_hash="unused-test-hash",
            role="user",
            active=False,
        )
        db.session.add(user)
        db.session.commit()
        user_id = user.id

        assert load_user(str(user_id)) is None


def test_ai_and_invoice_endpoints_require_explicit_employee_permissions():
    assert employee_endpoint_permission("whatsapp_agent.ai_whatsapp_attention", "GET") == "ai_access"
    assert employee_endpoint_permission("whatsapp_agent.ai_whatsapp_attention_action", "POST") == "ai_access"
    assert employee_endpoint_permission("whatsapp_agent.ai_whatsapp_attention_message", "POST") == "ai_access"
    assert employee_endpoint_permission("resolve_invoice_code_direct", "POST") == "ai_access"
    assert employee_endpoint_permission("dashboard.resolve_invoice_line", "POST") == "ai_access"
    assert employee_endpoint_permission("dashboard.process_invoice", "POST") == "ai_access"


def test_backup_restore_does_not_restore_paid_ai_entitlements():
    current = {
        "timezone": "America/Argentina/Cordoba",
        "ai_agent": {"plan_code": "inicio", "status": "VENCIDA", "enabled": False},
    }
    backup = {
        "timezone": "UTC",
        "ai_agent": {"plan_code": "pro", "status": "ACTIVA", "enabled": True},
        "layout": "compact",
    }

    restored = json.loads(
        BackupService._preserve_ai_entitlement_preferences(
            current_raw=json.dumps(current),
            restored_raw=json.dumps(backup),
        )
    )

    assert restored["timezone"] == "UTC"
    assert restored["layout"] == "compact"
    assert restored["ai_agent"] == current["ai_agent"]


def test_backup_cannot_invent_ai_entitlement_if_tenant_has_none():
    backup = {
        "timezone": "UTC",
        "ai_agent": {"plan_code": "pro", "status": "ACTIVA", "enabled": True},
    }

    restored = json.loads(
        BackupService._preserve_ai_entitlement_preferences(
            current_raw=json.dumps({"timezone": "America/Argentina/Cordoba"}),
            restored_raw=json.dumps(backup),
        )
    )

    assert restored == {"timezone": "UTC"}
