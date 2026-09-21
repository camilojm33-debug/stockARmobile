import os

from app import Client, Company, Product, db
from services.backup_service import BackupService


def test_backup_restore_round_trip_rolls_back_changes(app, monkeypatch):
    monkeypatch.setenv("BACKUP_UPLOAD_DIR", str((__import__("pathlib").Path(app.instance_path) / "qa-backups")))
    with app.app_context():
        company = Company(name="Backup QA", active=True)
        db.session.add(company)
        db.session.flush()

        db.session.add(
            Product(
                company_id=company.id,
                barcode="BKP-001",
                name="Producto QA",
                price=123.45,
                stock=7,
                active=True,
            )
        )
        db.session.add(
            Client(
                company_id=company.id,
                name="Cliente QA",
                phone="123",
            )
        )
        db.session.commit()

        backup, _plan = BackupService.create_manual_backup(company.id, user_id=None)
        db.session.commit()

        result = BackupService.verify_restore_round_trip(
            backup,
            expected_company_id=company.id,
        )
        assert result["valid"] is True
        assert result["rolled_back"] is True
        assert result["counts_before"]["products"] == 1
        assert result["counts_before"]["clients"] == 1
        assert backup.status == "ready"


def test_backup_maintenance_endpoint_requires_token(app, monkeypatch):
    monkeypatch.delenv("BACKUP_AUTOMATION_TOKEN", raising=False)
    client = app.test_client()
    response = client.post("/internal/maintenance/backups/run")
    assert response.status_code == 403


def test_backup_maintenance_endpoint_accepts_valid_token_and_processes_companies(app, monkeypatch):
    monkeypatch.setenv("BACKUP_AUTOMATION_TOKEN", "test-token")
    company = None
    with app.app_context():
        company = Company(name="Maintenance QA", active=True)
        db.session.add(company)
        db.session.commit()

    calls = []

    def fake_backup(company_id, *, user_id, trigger_type):
        class FakeBackup:
            id = 123
            file_name = "backup.qa.gz"

        calls.append((company_id, user_id, trigger_type))
        return FakeBackup(), {"code": "trial"}

    monkeypatch.setattr(BackupService, "create_manual_backup", staticmethod(fake_backup))
    client = app.test_client()
    response = client.post(
        "/internal/maintenance/backups/run",
        headers={"X-Backup-Automation-Token": "test-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls
    assert calls[0][0] == company.id
    assert calls[0][1] is None
    assert calls[0][2] == "automated_render"
