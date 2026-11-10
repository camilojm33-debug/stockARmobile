import json

from app import Branch, Company, Quote, QuoteDelivery, User, db
from branches import _has_pending_vendor_orders


def test_deactivation_guard_detects_pending_vendor_ai_order_and_shipping(app):
    with app.app_context():
        company = Company(name="Empresa pedidos IA", active=True)
        db.session.add(company)
        db.session.flush()
        branch = Branch(
            company_id=company.id,
            name="Sucursal pedidos",
            code="PEDIDOS",
            active=True,
            is_default=False,
        )
        user = User(
            username="branch-guard-admin",
            email="branch-guard@example.test",
            password_hash="not-used",
            company_id=company.id,
            role="admin",
        )
        db.session.add_all([branch, user])
        db.session.flush()
        quote = Quote(
            number="AI-GUARD-001",
            company_id=company.id,
            branch_id=branch.id,
            created_by_user_id=user.id,
            observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
            status="pendiente",
        )
        db.session.add(quote)
        db.session.commit()

        assert _has_pending_vendor_orders(company.id, branch.id) is True

        quote.status = "convertido"
        delivery = QuoteDelivery(
            quote_id=quote.id,
            company_id=company.id,
            method="envio",
            shipping_status="pending",
        )
        db.session.add(delivery)
        db.session.commit()
        assert _has_pending_vendor_orders(company.id, branch.id) is True

        delivery.shipping_status = "confirmed"
        delivery.pickup_status = "pending"
        db.session.commit()
        assert _has_pending_vendor_orders(company.id, branch.id) is False


def test_deactivation_guard_ignores_quotes_not_created_by_vendor_ai(app):
    with app.app_context():
        company = Company(name="Empresa presupuestos", active=True)
        db.session.add(company)
        db.session.flush()
        branch = Branch(
            company_id=company.id,
            name="Sucursal presupuestos",
            code="PRESUPUESTOS",
            active=True,
            is_default=False,
        )
        user = User(
            username="branch-guard-user",
            email="branch-guard-user@example.test",
            password_hash="not-used",
            company_id=company.id,
            role="admin",
        )
        db.session.add_all([branch, user])
        db.session.flush()
        quote = Quote(
            number="PRES-001",
            company_id=company.id,
            branch_id=branch.id,
            created_by_user_id=user.id,
            observations="Presupuesto creado manualmente",
            status="pendiente",
        )
        db.session.add(quote)
        db.session.commit()

        assert _has_pending_vendor_orders(company.id, branch.id) is False
