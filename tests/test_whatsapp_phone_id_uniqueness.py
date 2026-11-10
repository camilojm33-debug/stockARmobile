import json

import pytest

from app import Branch, Company, db
from services.ai_agent.config_service import configure_whatsapp_connection


def _company(name):
    company = Company(
        name=name,
        active=True,
        preferences_json=json.dumps({"ai_agent": {}}),
    )
    db.session.add(company)
    db.session.flush()
    branch = Branch(
        company_id=company.id,
        name="Casa central",
        code=f"CENTRAL-{company.id}",
        active=True,
        is_default=True,
    )
    db.session.add(branch)
    db.session.flush()
    return company, branch


def test_whatsapp_phone_id_cannot_be_reused_by_another_branch_or_tenant(app):
    with app.app_context():
        first_company, first_branch = _company("WhatsApp empresa uno")
        second_company, second_branch = _company("WhatsApp empresa dos")

        configure_whatsapp_connection(
            first_company,
            phone_number_id="123456789000",
            access_token="test-token",
            enabled=True,
        )
        db.session.commit()

        with pytest.raises(ValueError, match="ya está conectado"):
            configure_whatsapp_connection(
                second_company,
                phone_number_id="123456789000",
                access_token="other-token",
                enabled=True,
                branch_id=second_branch.id,
            )
        db.session.rollback()

        # Updating the same existing assignment is permitted.
        configure_whatsapp_connection(
            first_company,
            phone_number_id="123456789000",
            access_token="rotated-token",
            enabled=True,
        )
        db.session.commit()

        assert first_branch.company_id == first_company.id
        assert second_branch.company_id == second_company.id


def test_whatsapp_phone_id_can_be_assigned_to_a_different_number(app):
    with app.app_context():
        first_company, _first_branch = _company("WhatsApp empresa A")
        second_company, second_branch = _company("WhatsApp empresa B")

        configure_whatsapp_connection(
            first_company,
            phone_number_id="111111111111",
            access_token="token-a",
            enabled=True,
        )
        configure_whatsapp_connection(
            second_company,
            phone_number_id="222222222222",
            access_token="token-b",
            enabled=True,
            branch_id=second_branch.id,
        )
        db.session.commit()

        assert second_branch.company_id == second_company.id
