"""PostgreSQL-backed concurrency smoke test for 100 isolated tenant dashboards.

This deliberately exercises the application/ORM with 100 distinct tenant
sessions. It is not a substitute for a network-level production load test.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from statistics import median
from threading import Barrier
from time import perf_counter

import pytest

from app import Client, Company, CompanyMembership, Product, Sale, SaleItem, User, db, utcnow


TENANT_COUNT = 100
REQUEST_TIMEOUT_SECONDS = 45


def test_one_hundred_tenants_can_load_their_own_dashboard_concurrently(app):
    """Serve a synchronized burst of dashboard requests without cross-tenant data."""
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("La prueba de capacidad requiere PostgreSQL real.")

        trial_ends_at = utcnow() + timedelta(days=30)
        tenants = []

        for index in range(TENANT_COUNT):
            marker = f"LOAD_TENANT_{index:03d}"
            company = Company(
                name=f"Load test {marker}",
                active=True,
                trial_ends_at=trial_ends_at,
                timezone="America/Argentina/Buenos_Aires",
            )
            db.session.add(company)
            db.session.flush()

            user = User(
                username=f"load_admin_{index:03d}",
                email=f"load_admin_{index:03d}@example.test",
                password_hash="benchmark-only-not-a-login-password",
                role="admin",
                active=True,
                company_id=company.id,
            )
            product = Product(
                company_id=company.id,
                barcode=f"LOAD-{index:03d}",
                name=f"PRODUCT_{marker}",
                category="Benchmark",
                stock=1,
                min_stock=3,
                cost_price=10,
                price=25,
                active=True,
            )
            client = Client(
                name=f"CLIENT_{marker}",
                email=f"client_{index:03d}@example.test",
                active=True,
                company_id=company.id,
            )
            db.session.add_all([user, product, client])
            db.session.flush()
            membership = CompanyMembership(
                user_id=user.id,
                company_id=company.id,
                role="admin",
                permissions_json='["sales","inventory","cash","reports","quotes_view"]',
                active=True,
                status="active",
                accepted_at=utcnow(),
            )
            db.session.add(membership)

            sale = Sale(
                date=utcnow(),
                customer=client.name,
                subtotal=25,
                total_amount=25,
                paid_amount=25,
                payment_method="EFECTIVO",
                status="confirmada",
                client_id=client.id,
                seller_id=user.id,
                company_id=company.id,
            )
            db.session.add(sale)
            db.session.flush()
            db.session.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=product.id,
                    quantity=1,
                    price=25,
                    cost_price=10,
                )
            )
            tenants.append(
                {
                    "company_id": company.id,
                    "user_id": user.id,
                    "product_marker": product.name,
                    "client_marker": client.name,
                }
            )

        db.session.commit()
        db.session.remove()

    start_gate = Barrier(TENANT_COUNT)
    all_product_markers = [tenant["product_marker"] for tenant in tenants]
    all_client_markers = [tenant["client_marker"] for tenant in tenants]

    def load_dashboard(tenant):
        client = app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_user_id"] = str(tenant["user_id"])
            browser_session["_fresh"] = True

        # Align all 100 requests so the database sees a concurrent burst rather
        # than a sequential loop that merely visits 100 tenant accounts.
        start_gate.wait(timeout=REQUEST_TIMEOUT_SECONDS)
        started_at = perf_counter()
        response = client.get("/dashboard/")
        elapsed_seconds = perf_counter() - started_at
        html = response.get_data(as_text=True)
        client.close()

        assert response.status_code == 200, (
            f"Tenant {tenant['company_id']} recibió HTTP {response.status_code}: "
            f"{html[:300]}"
        )
        assert f'data-company-id="{tenant["company_id"]}"' in html
        assert f'data-user-id="{tenant["user_id"]}"' in html
        assert tenant["product_marker"] in html
        assert tenant["client_marker"] in html

        foreign_markers = [
            marker for marker in all_product_markers
            if marker != tenant["product_marker"]
        ] + [
            marker for marker in all_client_markers
            if marker != tenant["client_marker"]
        ]
        leaked_markers = [marker for marker in foreign_markers if marker in html]
        assert not leaked_markers, (
            f"El dashboard del tenant {tenant['company_id']} expuso datos de otro comercio: "
            f"{leaked_markers[:3]}"
        )

        return elapsed_seconds

    with ThreadPoolExecutor(max_workers=TENANT_COUNT) as executor:
        futures = [executor.submit(load_dashboard, tenant) for tenant in tenants]
        durations = [
            future.result(timeout=REQUEST_TIMEOUT_SECONDS)
            for future in futures
        ]

    ordered = sorted(durations)
    p50_seconds = median(ordered)
    p95_seconds = ordered[int((TENANT_COUNT - 1) * 0.95)]
    p99_seconds = ordered[int((TENANT_COUNT - 1) * 0.99)]
    max_seconds = ordered[-1]

    print(
        "TENANT_CAPACITY_BENCHMARK "
        f"tenants={TENANT_COUNT} requests={len(durations)} "
        f"success={len(durations)} errors=0 "
        f"p50={p50_seconds:.3f}s p95={p95_seconds:.3f}s "
        f"p99={p99_seconds:.3f}s max={max_seconds:.3f}s"
    )

    # Keep this as a broad regression bound, not a claimed production SLA.
    # Each request has a 45 s hard timeout; any HTTP, tenant-scope, DB-pool,
    # or execution failure above has already failed the test.
    assert p95_seconds < REQUEST_TIMEOUT_SECONDS
    assert p99_seconds < REQUEST_TIMEOUT_SECONDS
    with app.app_context():
        assert db.engine.pool.checkedout() == 0
