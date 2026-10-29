"""Regression tests for tenant-safe AI-order channel attribution."""

import pytest

import whatsapp_agent
from app import Company, Payment, Quote, User, db
from stockarmobile.models.conversations import Conversation


def _create_order_reference(*, channel=None, foreign_conversation=False, include_conversation=True):
    company = Company(name="Canal QA A", active=True)
    db.session.add(company)
    db.session.flush()

    user = User(
        username="channel_qa_admin",
        email="channel-qa-admin@example.test",
        password_hash="unused-test-hash",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()  # Quote.created_by_user_id is non-nullable.

    conversation_company = company
    if foreign_conversation:
        conversation_company = Company(name="Canal QA B", active=True)
        db.session.add(conversation_company)
        db.session.flush()

    conversation = None
    if channel is not None:
        conversation = Conversation(
            company_id=conversation_company.id,
            channel=channel,
            status="open",
        )
        db.session.add(conversation)
        db.session.flush()

    quote = Quote(
        number="QA-CHANNEL-001",
        company_id=company.id,
        created_by_user_id=user.id,
        observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
        total_amount=100,
        currency="ARS",
        status="ENVIADO",
    )
    db.session.add(quote)
    db.session.flush()

    external_reference = (
        f"flow:ai_order|company_id:{company.id}|quote_id:{quote.id}|"
    )
    if include_conversation and conversation is not None:
        external_reference += f"conversation_id:{conversation.id}"

    payment = Payment(
        company_id=company.id,
        provider="mercadopago_ai_order",
        external_reference=external_reference,
        status="pending",
        amount=100,
        currency="ARS",
    )
    db.session.add(payment)
    db.session.commit()
    return company, quote, conversation


@pytest.mark.parametrize("channel", ["whatsapp", "webchat"])
def test_ai_order_channel_uses_tenant_scoped_origin_conversation(app, channel):
    with app.app_context():
        company, quote, _conversation = _create_order_reference(channel=channel)

        assert whatsapp_agent._ai_order_channel(company.id, quote.id) == channel


def test_ai_order_channel_does_not_mislabel_explicit_foreign_conversation_as_webchat(app):
    with app.app_context():
        company, quote, _conversation = _create_order_reference(
            channel="whatsapp",
            foreign_conversation=True,
        )

        assert whatsapp_agent._ai_order_channel(company.id, quote.id) == "unknown"


def test_legacy_ai_order_without_conversation_id_keeps_webchat_compatibility(app):
    with app.app_context():
        company, quote, _conversation = _create_order_reference(
            channel=None,
            include_conversation=False,
        )

        assert whatsapp_agent._ai_order_channel(company.id, quote.id) == "webchat"
