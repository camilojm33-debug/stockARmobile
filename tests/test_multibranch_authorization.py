from types import SimpleNamespace

import pytest
from werkzeug.exceptions import Forbidden

import whatsapp_agent
from app import Branch, Company, Quote, db
from stockarmobile import branch_context
from stockarmobile.permissions import employee_endpoint_permission


def _tenant_with_branches():
    company = Company(name="Empresa multisucursal QA", active=True)
    db.session.add(company)
    db.session.flush()
    first = Branch(
        company_id=company.id,
        name="Local norte",
        code=f"NORTE-{company.id}",
        active=True,
        is_default=True,
    )
    second = Branch(
        company_id=company.id,
        name="Local sur",
        code=f"SUR-{company.id}",
        active=True,
        is_default=False,
    )
    db.session.add_all([first, second])
    db.session.flush()
    return company, first, second


def test_vendor_ai_scope_limits_employee_to_assigned_branches(app, monkeypatch):
    with app.app_context():
        company, assigned, _other = _tenant_with_branches()
        monkeypatch.setattr(whatsapp_agent, "current_user", SimpleNamespace(role="user"))
        monkeypatch.setattr(
            branch_context,
            "accessible_branches",
            lambda company_id=None: [assigned],
        )

        scoped = whatsapp_agent._scope_ai_branch(
            db.session.query(Quote), Quote, company.id
        )
        sql = str(scoped.statement.compile(compile_kwargs={"literal_binds": True}))

        assert "quotes.branch_id IN" in sql
        assert str(assigned.id) in sql


def test_vendor_ai_scope_denies_employee_without_branch_assignment(app, monkeypatch):
    with app.app_context():
        company, _assigned, _other = _tenant_with_branches()
        monkeypatch.setattr(whatsapp_agent, "current_user", SimpleNamespace(role="user"))
        monkeypatch.setattr(branch_context, "accessible_branches", lambda company_id=None: [])

        with pytest.raises(Forbidden):
            whatsapp_agent._ai_assigned_branch_ids(company.id)


def test_vendor_ai_admin_scope_remains_company_wide(app, monkeypatch):
    with app.app_context():
        company, _first, _second = _tenant_with_branches()
        monkeypatch.setattr(whatsapp_agent, "current_user", SimpleNamespace(role="admin"))
        monkeypatch.setattr(
            branch_context,
            "accessible_branches",
            lambda company_id=None: (_ for _ in ()).throw(AssertionError("admin must not use employee branch filter")),
        )

        query = db.session.query(Quote)
        scoped = whatsapp_agent._scope_ai_branch(query, Quote, company.id)

        assert scoped.statement.whereclause is None


def test_pdf_ticket_endpoint_requires_sales_permission_not_inventory():
    assert employee_endpoint_permission("qr_labels.generate_pdf_ticket", "GET") == "sales"
    assert employee_endpoint_permission("qr_labels.generate_product_labels", "GET") == "inventory"
