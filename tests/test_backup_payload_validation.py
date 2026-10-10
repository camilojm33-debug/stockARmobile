import copy

import pytest

from services.backup_service import BackupService


def valid_backup_payload():
    return {
        "schema_version": 2,
        "company_id": 42,
        "users": [{"id": 1, "company_id": 42, "role": "admin"}],
        "products": [{"id": 10, "company_id": 42, "supplier_id": 7}],
        "clients": [{"id": 20, "company_id": 42}],
        "suppliers": [{"id": 7, "company_id": 42}],
        "sales": [{"id": 30, "company_id": 42, "client_id": 20, "cash_session_id": 50}],
        "sale_items": [{"id": 40, "sale_id": 30, "product_id": 10}],
        "purchase_orders": [{"id": 60, "company_id": 42, "supplier_id": 7}],
        "purchase_items": [{"id": 70, "purchase_order_id": 60, "product_id": 10}],
        "cash_sessions": [{"id": 50, "company_id": 42}],
        "cash_movements": [{"id": 80, "company_id": 42, "session_id": 50, "sale_id": 30}],
        "expenses": [{"id": 90, "company_id": 42}],
    }


def test_backup_tenant_validator_accepts_rows_belonging_to_target_company():
    payload = valid_backup_payload()

    assert BackupService._validate_backup_tenant_scope(payload, 42) is None


def test_backup_tenant_validator_rejects_rows_from_another_company():
    payload = valid_backup_payload()
    payload["products"][0]["company_id"] = 99

    with pytest.raises(ValueError, match="otra empresa"):
        BackupService._validate_backup_tenant_scope(payload, 42)


def test_backup_tenant_validator_rejects_superadmin_users():
    payload = valid_backup_payload()
    payload["users"][0]["role"] = " SuperAdmin "

    with pytest.raises(ValueError, match="superadmin"):
        BackupService._validate_backup_tenant_scope(payload, 42)


def test_backup_tenant_validator_rejects_child_rows_linked_outside_backup():
    payload = valid_backup_payload()
    payload["sale_items"][0]["sale_id"] = 999

    with pytest.raises(ValueError, match="referencia fuera"):
        BackupService._validate_backup_tenant_scope(payload, 42)


def test_backup_tenant_validator_rejects_missing_company_id_on_tenant_rows():
    payload = valid_backup_payload()
    payload["products"][0].pop("company_id")

    with pytest.raises(ValueError, match="no identifica su empresa"):
        BackupService._validate_backup_tenant_scope(payload, 42)


def test_backup_tenant_validator_rejects_malformed_row_sections():
    payload = valid_backup_payload()
    payload["users"] = {"id": 1, "company_id": 42, "role": "admin"}

    with pytest.raises(ValueError, match="formato invalido"):
        BackupService._validate_backup_tenant_scope(payload, 42)


def test_backup_tenant_validator_rejects_a_backup_for_another_company():
    payload = valid_backup_payload()
    payload["company_id"] = 41

    with pytest.raises(ValueError, match="no corresponde"):
        BackupService._validate_backup_tenant_scope(payload, 42)
