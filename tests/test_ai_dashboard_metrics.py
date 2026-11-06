from datetime import datetime, timedelta

from app import Branch, Company, Quote, Sale, User, db
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
    assert "('Pedidos IA', vendor_branch_metrics.orders" in template
    assert "('Ventas derivadas', vendor_branch_metrics.ai_sales" in template
    assert "Cupo mensual compartido por toda la empresa" in template
    assert "data-copy-vendor-url" in template
    assert "branch_status.web_active" in template
    assert "('Hacer seguimiento', false)" not in template



def test_vendor_dashboard_metrics_are_scoped_to_selected_branch(app):
    with app.app_context():
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        company = Company(name="Empresa métricas por sucursal", active=True)
        db.session.add(company)
        db.session.flush()

        central = Branch(
            company_id=company.id, name="Casa central", code="CENTRAL",
            active=True, is_default=True,
        )
        north = Branch(
            company_id=company.id, name="Sucursal Norte", code="NORTE",
            active=True, is_default=False,
        )
        db.session.add_all([central, north])
        db.session.flush()

        user = User(
            username="branch_metric_user",
            email="branch_metric_user@test.local",
            role="admin",
            company_id=company.id,
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        central_sale = Sale(
            company_id=company.id,
            branch_id=central.id,
            date=now,
            created_at=now,
            status="confirmada",
            total_amount=1000,
        )
        north_sale = Sale(
            company_id=company.id,
            branch_id=north.id,
            date=now,
            created_at=now,
            status="confirmada",
            total_amount=2500,
        )
        db.session.add_all([central_sale, north_sale])
        db.session.flush()

        db.session.add_all([
            Quote(
                company_id=company.id,
                branch_id=central.id,
                created_at=now,
                date=now,
                number="P-CENTRAL-1",
                observations=VENDOR_AI_ORDER_OBSERVATION,
                status="ENVIADO",
                created_by_user_id=user.id,
                total_amount=1000,
                converted_sale_id=central_sale.id,
            ),
            Quote(
                company_id=company.id,
                branch_id=north.id,
                created_at=now,
                date=now,
                number="P-NORTE-1",
                observations=VENDOR_AI_ORDER_OBSERVATION,
                status="ENVIADO",
                created_by_user_id=user.id,
                total_amount=2500,
                converted_sale_id=north_sale.id,
            ),
        ])
        db.session.commit()

        north_metrics = _vendor_dashboard_metrics(company.id, month_start, branch_id=north.id)
        central_metrics = _vendor_dashboard_metrics(company.id, month_start, branch_id=central.id)
        company_metrics = _vendor_dashboard_metrics(company.id, month_start)

        assert north_metrics == {"orders": 1, "ai_sales": 1}
        assert central_metrics == {"orders": 1, "ai_sales": 1}
        assert company_metrics == {"orders": 2, "ai_sales": 2}
        db.session.rollback()
