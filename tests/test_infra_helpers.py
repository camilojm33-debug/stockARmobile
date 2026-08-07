from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from flask import Flask

from stockarmobile import audit
from stockarmobile.constants import ROLE_ADMIN, ROLE_SUPERADMIN
from stockarmobile.enums import QuoteStatus, SaleStatus, UserRole
from stockarmobile.helpers.dates import parse_date_yyyy_mm_dd, utcnow_naive
from stockarmobile.helpers.money import safe_decimal
from stockarmobile.helpers.numbers import safe_float, safe_int
from stockarmobile.helpers.pagination import resolve_pagination
from stockarmobile.helpers.strings import is_safe_relative_redirect, normalize_digits, normalize_lower, normalize_text
from stockarmobile.helpers.validators import is_positive_number, is_valid_email
from stockarmobile.permissions import has_any_permission, is_admin, is_superadmin, parse_permissions_json
from stockarmobile.responses import api_error, api_success
from stockarmobile.tenant import get_current_company_id, is_control_panel_owner, scope_query_to_company


class _FakeQuery:
    def __init__(self):
        self.filtered_with = None

    def filter(self, expression):
        self.filtered_with = expression
        return self


class _ExprField:
    def __eq__(self, other):
        return ("eq", other)


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


class _FakeAuditLog:
    def __init__(self, **kwargs):
        self.payload = kwargs


def test_numbers_helpers():
    assert safe_float("12.5") == 12.5
    assert safe_float(None, 7.0) == 7.0
    assert safe_float("x", 2.0) == 2.0
    assert safe_int("4") == 4
    assert safe_int("x", 9) == 9


def test_money_helper():
    assert safe_decimal("10.50") == Decimal("10.50")
    assert safe_decimal(None, "3.00") == Decimal("3.00")
    assert safe_decimal("bad", "1.25") == Decimal("1.25")


def test_dates_helpers():
    parsed = parse_date_yyyy_mm_dd("2026-08-06")
    assert isinstance(parsed, datetime)
    assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 6
    assert parse_date_yyyy_mm_dd("not-a-date") is None
    now_value = utcnow_naive()
    assert now_value.tzinfo is None


def test_strings_helpers():
    assert normalize_text("  abc  ") == "abc"
    assert normalize_lower("  AbC  ") == "abc"
    assert normalize_digits("+54 9 11-2233") == "549112233"
    assert is_safe_relative_redirect("/dashboard") is True
    assert is_safe_relative_redirect("https://evil.example") is False


def test_validators_helpers():
    assert is_valid_email("user@example.com") is True
    assert is_valid_email("bad@") is False
    assert is_positive_number("1.1") is True
    assert is_positive_number("0") is False


def test_pagination_helper():
    page, per_page = resolve_pagination("2", "500", max_per_page=100)
    assert page == 2
    assert per_page == 100


def test_permissions_helpers():
    admin = SimpleNamespace(role="admin", permissions_json='["sales", "reports"]')
    superadmin = SimpleNamespace(role="superadmin", permissions_json="[]")
    assert is_admin(admin) is True
    assert is_superadmin(superadmin) is True
    assert parse_permissions_json(admin.permissions_json) == {"sales", "reports"}
    assert has_any_permission(admin, {"reports"}) is True


def test_tenant_helpers_scope_and_context(monkeypatch):
    user = SimpleNamespace(is_authenticated=True, role=ROLE_ADMIN, company_id=10)
    superadmin = SimpleNamespace(is_authenticated=True, role=ROLE_SUPERADMIN, company_id=99)
    anon = SimpleNamespace(is_authenticated=False, role=None, company_id=None)

    assert get_current_company_id(user) == 10
    assert get_current_company_id(superadmin) is None
    assert get_current_company_id(anon) is None

    fake_query = _FakeQuery()
    fake_model = SimpleNamespace(company_id=_ExprField())
    result = scope_query_to_company(fake_query, fake_model, current_user_obj=user)
    assert result is fake_query
    assert fake_query.filtered_with == ("eq", 10)

    fake_query_no_company = _FakeQuery()
    user_without_company = SimpleNamespace(is_authenticated=True, role=ROLE_ADMIN, company_id=None)
    scope_query_to_company(fake_query_no_company, fake_model, current_user_obj=user_without_company)
    assert fake_query_no_company.filtered_with is not None

    monkeypatch.setenv("ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    owner_user = SimpleNamespace(username="owner", email="other@example.com")
    assert is_control_panel_owner(owner_user) is True


def test_response_helpers_json_shape():
    app = Flask(__name__)
    with app.app_context():
        response, status = api_error("boom", 409, code="conflict")
        assert status == 409
        assert response.get_json() == {"success": False, "error": "boom", "code": "conflict"}

        ok_response, ok_status = api_success(201, data=123)
        assert ok_status == 201
        assert ok_response.get_json() == {"success": True, "data": 123}


def test_audit_helper_builds_payload(monkeypatch):
    fake_session = _FakeSession()
    monkeypatch.setattr(audit, "current_user", SimpleNamespace(is_authenticated=True, id=7))
    monkeypatch.setattr(audit, "request", SimpleNamespace(remote_addr="127.0.0.1"))

    audit.record_audit_entry(
        fake_session,
        _FakeAuditLog,
        lambda: 22,
        action="test_action",
        entity="entity",
        detail="detail",
    )

    assert len(fake_session.added) == 1
    payload = fake_session.added[0].payload
    assert payload["user_id"] == 7
    assert payload["company_id"] == 22
    assert payload["action"] == "test_action"
    assert payload["ip_address"] == "127.0.0.1"


def test_enums_keep_public_values():
    assert UserRole.ADMIN.value == "admin"
    assert SaleStatus.CONFIRMED.value == "confirmada"
    assert QuoteStatus.APROBADO.value == "APROBADO"
