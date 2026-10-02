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
        company_id = company.id

    calls = []

    def fake_backup(company_id, *, user_id, trigger_type):
        class FakeBackup:
            id = 123
            file_name = "backup.qa.gz"

        calls.append((company_id, user_id, trigger_type))
        return FakeBackup(), {"code": "trial"}

    monkeypatch.setattr(
        BackupService,
        "automated_backup_due",
        staticmethod(lambda company_id, *, day_of_month=None: True),
    )
    monkeypatch.setattr(BackupService, "create_manual_backup", staticmethod(fake_backup))
    client = app.test_client()
    response = client.post(
        "/internal/maintenance/backups/run",
        headers={"X-Backup-Automation-Token": "test-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls
    assert calls[0][0] == company_id
    assert calls[0][1] is None
    assert calls[0][2] == "automated_render"


def test_gmail_watch_maintenance_requires_token(app, monkeypatch):
    monkeypatch.delenv("GMAIL_WATCH_AUTOMATION_TOKEN", raising=False)
    client = app.test_client()
    response = client.post("/internal/maintenance/gmail/watch")
    assert response.status_code == 403


def test_gmail_watch_maintenance_renews_with_valid_token(app, monkeypatch):
    monkeypatch.setenv("GMAIL_WATCH_AUTOMATION_TOKEN", "gmail-test-token")
    calls = []

    def fake_renew_watch():
        calls.append(True)
        return {"status": "renewed", "history_id": "123", "expiration": "456"}

    monkeypatch.setattr(
        "services.gmail_commercial_service.renew_watch",
        fake_renew_watch,
    )
    client = app.test_client()
    response = client.post(
        "/internal/maintenance/gmail/watch",
        headers={"X-Gmail-Watch-Automation-Token": "gmail-test-token"},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [True]

    
def test_backup_monthly_slots_match_subscription_tiers():
    assert BackupService.monthly_backup_slots("trial") == (1,)
    assert BackupService.monthly_backup_slots("entrepreneur") == (1,)
    assert BackupService.monthly_backup_slots("business") == (1, 15)
    assert BackupService.monthly_backup_slots("premium") == (1, 10, 20)


def test_backup_due_only_on_plan_monthly_slots(app, monkeypatch):
    with app.app_context():
        company = Company(name="Schedule QA", active=True)
        db.session.add(company)
        db.session.commit()

        monkeypatch.setattr(
            BackupService,
            "_plan_context",
            staticmethod(lambda company_id: {"code": "business", "name": "Negocio", "limit": 2}),
        )

        assert BackupService.automated_backup_due(company.id, day_of_month=1) is True
        assert BackupService.automated_backup_due(company.id, day_of_month=15) is True
        assert BackupService.automated_backup_due(company.id, day_of_month=10) is False
