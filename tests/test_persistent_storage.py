from pathlib import Path

from services.backup_service import BackupService
from services.invoice_upload_service import InvoiceUploadService


def test_invoice_upload_dir_is_configurable(tmp_path, app):
    app.config["INVOICE_UPLOAD_DIR"] = str(tmp_path / "invoices")
    app.config["IS_PRODUCTION_ENV"] = True

    with app.app_context():
        path = InvoiceUploadService._directory(42)

    assert path == Path(tmp_path / "invoices" / "companies" / "42" / "invoices")


def test_backup_root_is_configurable(tmp_path, app):
    app.config["BACKUP_UPLOAD_DIR"] = str(tmp_path / "backups")
    app.config["IS_PRODUCTION_ENV"] = True

    with app.app_context():
        path = BackupService._backup_root()

    assert path == Path(tmp_path / "backups")
    assert path.is_dir()


def test_persistent_services_have_render_fallbacks(monkeypatch, app):
    monkeypatch.setenv("RENDER", "true")
    app.config["INVOICE_UPLOAD_DIR"] = ""
    app.config["BACKUP_UPLOAD_DIR"] = ""

    monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)

    with app.app_context():
        invoice_path = InvoiceUploadService._directory(7)
        backup_path = BackupService._backup_root()

    assert str(invoice_path).startswith("/var/data/stockarmobile/invoices/")
    assert str(backup_path) == "/var/data/stockarmobile/backups"
