from datetime import timedelta

from flask_login import login_user, logout_user

import app as stock_app
from app import Company, Sale, User, db
from services.dashboard_service import _sales_by_day
from stockarmobile.helpers.dates import local_day_bounds_utc_naive, local_today


def test_sales_by_day_uses_local_company_days(app):
    with app.app_context():
        company = Company(name="Performance Test", active=True, timezone="America/Argentina/Buenos_Aires")
        db.session.add(company)
        db.session.flush()

        user = User(
            username="performance_admin",
            email="performance@test.local",
            role="admin",
            company_id=company.id,
            active=True,
        )
        user.set_password("admin123")
        db.session.add(user)
        db.session.flush()

        today = local_today(company.timezone)
        yesterday = today - timedelta(days=1)
        yesterday_start, yesterday_end = local_day_bounds_utc_naive(yesterday, company.timezone)
        today_start, today_end = local_day_bounds_utc_naive(today, company.timezone)

        db.session.add_all(
            [
                Sale(
                    date=yesterday_start + timedelta(hours=2),
                    subtotal=100,
                    total_amount=100,
                    paid_amount=100,
                    payment_method="EFECTIVO",
                    status="confirmada",
                    company_id=company.id,
                    seller_id=user.id,
                ),
                Sale(
                    date=today_start + timedelta(hours=2),
                    subtotal=250,
                    total_amount=250,
                    paid_amount=250,
                    payment_method="EFECTIVO",
                    status="confirmada",
                    company_id=company.id,
                    seller_id=user.id,
                ),
                Sale(
                    date=today_start + timedelta(hours=3),
                    subtotal=50,
                    total_amount=50,
                    paid_amount=50,
                    payment_method="EFECTIVO",
                    status="anulada",
                    company_id=company.id,
                    seller_id=user.id,
                ),
            ]
        )
        db.session.commit()

        login_user(user)
        try:
            values = _sales_by_day(2)
        finally:
            logout_user()

        assert [float(value) for value in values] == [100.0, 250.0]
