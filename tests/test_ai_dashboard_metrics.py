from datetime import datetime, timedelta

from app import Company, Quote, Sale, User, db
from ai_agents import VENDOR_AI_ORDER_OBSERVATION, _vendor_dashboard_metrics


def test_vendor_dashboard_metrics_use_real_ai_orders_and_sales(app):
    with app.app_context():
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        company = Company(name="Empresa métricas IA", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="metric_user",
            email="metric_user@test.local",
            role="admin",
            company_id=company.id,
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        derived_sale = Sale(
            company_id=company.id,
            date=now,
            created_at=now,
            status="confirmada",
            total_amount=1500,
        )
        db.session.add(derived_sale)
        db.session.flush()

        db.session.add(
            Quote(
                company_id=company.id,
                created_at=now,
                date=now,
                number="P-TEST-1",
                observations=VENDOR_AI_ORDER_OBSERVATION,
                status="ENVIADO",
                created_by_user_id=user.id,
                total_amount=1500,
                converted_sale_id=derived_sale.id,
            )
        )
        db.session.add(
            Quote(
                company_id=company.id,
                created_at=now,
                date=now,
                number="P-TEST-2",
                observations="Presupuesto manual",
                status="ENVIADO",
                created_by_user_id=user.id,
                total_amount=900,
            )
        )
        old_date = month_start - timedelta(days=1)
        db.session.add(
            Quote(
                company_id=company.id,
                created_at=old_date,
                date=old_date,
                number="P-OLD",
                observations=VENDOR_AI_ORDER_OBSERVATION,
                status="ENVIADO",
                created_by_user_id=user.id,
                total_amount=700,
            )
        )
        db.session.commit()

        metrics = _vendor_dashboard_metrics(company.id, month_start)

        assert metrics == {"orders": 1, "ai_sales": 1}


def test_vendor_dashboard_has_no_stale_pending_placeholders():
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "ai_agents"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "('Pedidos', 'Pendiente de integración')" not in template
    assert "('Ventas derivadas', 'Pendiente de integración')" not in template
    assert "('Pedidos', metrics.orders)" in template
    assert "('Ventas derivadas', metrics.ai_sales)" in template
    assert "('Hacer seguimiento', false)" not in template
