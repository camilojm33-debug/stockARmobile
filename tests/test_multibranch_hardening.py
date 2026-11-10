from pathlib import Path

import pytest

from app import Branch, Company, Quote, User, db
from stockarmobile.models.conversations import Conversation, WhatsAppChannelRegistration
from services.ai_agent.config_service import configure_whatsapp_connection


AI_MARKER = "Pedido generado por el Vendedor 24 hs de StockARmobile."


def _company(name):
    row = Company(name=name, active=True)
    db.session.add(row)
    db.session.flush()
    return row


def _branch(company, code, *, default=False):
    row = Branch(
        company_id=company.id,
        name=f"Sucursal {code}",
        code=code,
        active=True,
        is_default=default,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _user(company, username, role="admin"):
    row = User(
        username=username,
        email=f"{username}@example.test",
        password_hash="test-hash",
        role=role,
        active=True,
        company_id=company.id,
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_whatsapp_phone_number_id_is_reserved_for_one_tenant_branch(app):
    with app.app_context():
        company_a = _company("WhatsApp A")
        company_b = _company("WhatsApp B")
        branch_a = _branch(company_a, "WHA", default=True)
        branch_b = _branch(company_b, "WHB", default=True)
        db.session.commit()

        configure_whatsapp_connection(
            company_a, phone_number_id="987654321001", branch_id=branch_a.id,
            enabled=True, display_phone_number="+54 9 3624 000001",
        )
        db.session.commit()

        with pytest.raises(ValueError, match="otra empresa o sucursal"):
            configure_whatsapp_connection(
                company_b, phone_number_id="987654321001", branch_id=branch_b.id,
                enabled=True,
            )
        db.session.rollback()


def test_whatsapp_phone_number_can_be_updated_by_its_existing_owner(app):
    with app.app_context():
        company = _company("WhatsApp owner")
        branch = _branch(company, "OWNER", default=True)
        db.session.commit()

        configure_whatsapp_connection(
            company, phone_number_id="987654321002", branch_id=branch.id, enabled=True,
        )
        configure_whatsapp_connection(
            company, phone_number_id="987654321002", branch_id=branch.id, enabled=True,
        )
        db.session.commit()

        registration = db.session.get(WhatsAppChannelRegistration, "987654321002")
        assert registration is not None
        assert registration.company_id == company.id
        assert registration.branch_id == branch.id


def test_employee_vendor_queries_are_scoped_to_the_selected_branch(app, monkeypatch):
    from flask_login import login_user
    import whatsapp_agent

    with app.app_context():
        company = _company("Empresa por sucursal")
        branch_a = _branch(company, "A")
        branch_b = _branch(company, "B")
        employee = _user(company, "employee-branch-scope", role="user")
        quote_a = Quote(
            number="AI-BRANCH-A", company_id=company.id, branch_id=branch_a.id,
            created_by_user_id=employee.id, observations=AI_MARKER, status="PENDIENTE",
        )
        quote_b = Quote(
            number="AI-BRANCH-B", company_id=company.id, branch_id=branch_b.id,
            created_by_user_id=employee.id, observations=AI_MARKER, status="PENDIENTE",
        )
        conv_a = Conversation(
            company_id=company.id, branch_id=branch_a.id, channel="whatsapp",
            external_conversation_id="5493624000001",
        )
        conv_b = Conversation(
            company_id=company.id, branch_id=branch_b.id, channel="whatsapp",
            external_conversation_id="5493624000002",
        )
        db.session.add_all([quote_a, quote_b, conv_a, conv_b])
        db.session.commit()

        with app.test_request_context("/"):
            login_user(employee)
            monkeypatch.setattr(whatsapp_agent, "_active_employee_branch_id", lambda: branch_a.id)
            visible_quotes = whatsapp_agent._scope_vendor_query_to_active_branch(
                Quote.query.filter_by(company_id=company.id, observations=AI_MARKER), Quote
            ).all()
            visible_conversations = whatsapp_agent._scope_vendor_query_to_active_branch(
                Conversation.query.filter_by(company_id=company.id, channel="whatsapp"), Conversation
            ).all()

        assert [row.id for row in visible_quotes] == [quote_a.id]
        assert [row.id for row in visible_conversations] == [conv_a.id]


def test_branch_cannot_be_deactivated_with_an_unresolved_ai_order(app):
    import branches

    with app.app_context():
        company = _company("Empresa bloqueo sucursal")
        branch = _branch(company, "PENDING")
        owner = _user(company, "owner-pending-ai")
        quote = Quote(
            number="AI-PENDING-BLOCK", company_id=company.id, branch_id=branch.id,
            created_by_user_id=owner.id, observations=AI_MARKER, status="PENDIENTE",
        )
        db.session.add(quote)
        db.session.flush()

        assert branches._has_pending_ai_vendor_orders(company.id, branch.id) is True

        quote.status = "ANULADO"
        db.session.flush()
        assert branches._has_pending_ai_vendor_orders(company.id, branch.id) is False


def test_branch_cannot_be_deactivated_with_a_pending_ai_payment_even_if_quote_is_terminal(app):
    from app import Payment
    import branches

    with app.app_context():
        company = _company("Empresa pago IA pendiente")
        branch = _branch(company, "PAYMENT")
        owner = _user(company, "owner-ai-payment")
        quote = Quote(
            number="AI-PAYMENT-BLOCK", company_id=company.id, branch_id=branch.id,
            created_by_user_id=owner.id, observations=AI_MARKER, status="ANULADO",
        )
        db.session.add(quote)
        db.session.flush()
        db.session.add(Payment(
            company_id=company.id,
            provider="mercadopago_ai_order",
            status="pending",
            external_reference=f"flow:ai_order|company_id:{company.id}|quote_id:{quote.id}|conversation_id:5",
            amount=10,
            currency="ARS",
        ))
        db.session.flush()

        assert branches._has_pending_ai_vendor_orders(company.id, branch.id) is True


def test_test_database_safety_guard_runs_before_app_import():
    conftest = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    assert conftest.index("_configure_isolated_test_database()") < conftest.index("import app as stock_app")
    assert "tempfile.gettempdir()" in conftest
    assert "localhost" in conftest and "DATABASE_URL" in conftest
