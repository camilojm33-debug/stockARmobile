from __future__ import annotations

import json

import requests


def _response(status_code: int, content_type: str, body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.headers["Content-Type"] = content_type
    response._content = body
    return response


def test_maintenance_endpoints_keep_token_auth_when_csrf_is_enabled(app, monkeypatch):
    from services.backup_service import BackupService

    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    monkeypatch.setenv("BACKUP_AUTOMATION_TOKEN", "backup-test-token")
    monkeypatch.setenv("GMAIL_WATCH_AUTOMATION_TOKEN", "gmail-test-token")
    monkeypatch.setenv("BILLING_RECONCILIATION_TOKEN", "billing-reconcile-test-token")
    monkeypatch.setattr(
        BackupService,
        "automated_backup_due",
        staticmethod(lambda company_id, day_of_month=None: False),
    )
    monkeypatch.setattr(
        "services.gmail_commercial_service.renew_watch",
        lambda: {"status": "skipped"},
    )
    monkeypatch.setattr(
        "services.billing_reconciliation_service.BillingReconciliationService.run",
        lambda: {
            "ok": True, "skipped": False, "events_scanned": 2, "repaired": 1,
            "ignored": 1, "errors": 0, "attention": 0,
        },
    )
    client = app.test_client()

    for path, header, valid_token in (
        (
            "/internal/maintenance/backups/run",
            "X-Backup-Automation-Token",
            "backup-test-token",
        ),
        (
            "/internal/maintenance/gmail/watch",
            "X-Gmail-Watch-Automation-Token",
            "gmail-test-token",
        ),
        (
            "/internal/maintenance/billing/reconcile",
            "X-Billing-Reconciliation-Token",
            "billing-reconcile-test-token",
        ),
    ):
        missing = client.post(path)
        assert missing.status_code == 403
        assert missing.is_json
        assert missing.get_json()["error"] == "forbidden"

        invalid = client.post(path, headers={header: "incorrect-token"})
        assert invalid.status_code == 403
        assert invalid.is_json
        assert invalid.get_json()["error"] == "forbidden"

        accepted = client.post(path, headers={header: valid_token})
        assert accepted.status_code == 200
        assert accepted.is_json
        assert accepted.get_json()["ok"] is True


def test_render_health_check_targets_the_database_readiness_endpoint():
    from pathlib import Path

    render = Path("render.yaml").read_text(encoding="utf-8")
    app_source = Path("app.py").read_text(encoding="utf-8")
    assert "healthCheckPath: /readyz" in render
    assert 'app.route("/readyz", methods=["GET"])' in app_source
    assert "name: stockarmobile-daily-backup" in render
    assert 'schedule: "15 3 * * *"' in render
    assert "name: stockarmobile-billing-reconciliation" in render
    assert 'schedule: "*/15 * * * *"' in render
    assert "BILLING_RECONCILIATION_TOKEN" in render


def test_backup_cron_reports_only_safe_json_summary(capsys):
    from scripts.run_backup_maintenance import _report_response

    body = json.dumps(
        {
            "ok": True,
            "companies_processed": 3,
            "results": [
                {"status": "ready", "company_id": 901, "file_name": "private-backup.tar.gz"},
                {"status": "skipped", "company_id": 902},
                {"status": "ready", "company_id": 903, "file_name": "private-backup-2.tar.gz"},
            ],
            "restore_verification": None,
        }
    ).encode()
    response = _response(200, "application/json", body)

    assert _report_response(response) == 0
    output = capsys.readouterr().out
    assert "companies_processed=3" in output
    assert "ready=2" in output
    assert "skipped=1" in output
    assert "private-backup" not in output
    assert "901" not in output


def test_maintenance_cron_scripts_never_echo_html_csrf_response(capsys):
    from scripts.run_backup_maintenance import _report_response as backup_report
    from scripts.run_gmail_watch_maintenance import _report_response as gmail_report

    html = b"<html><meta name='csrf-token' content='sensitive-token'>Acceso denegado</html>"
    for report, label in (
        (backup_report, "Backup maintenance endpoint failed"),
        (gmail_report, "Gmail watch maintenance endpoint failed"),
    ):
        response = _response(403, "text/html; charset=utf-8", html)
        assert report(response) == 1
        stderr = capsys.readouterr().err
        assert label in stderr
        assert "403" in stderr
        assert "sensitive-token" not in stderr
        assert "csrf-token" not in stderr



def test_billing_reconciliation_cron_report_omits_response_bodies_and_secrets(capsys):
    from scripts.run_billing_reconciliation import _report_response

    response = _response(
        500,
        "text/html; charset=utf-8",
        b"<html>private-payment-id=123456 secret-token</html>",
    )
    assert _report_response(response) == 1
    error = capsys.readouterr().err
    assert "Billing reconciliation endpoint failed" in error
    assert "500" in error
    assert "private-payment-id" not in error
    assert "secret-token" not in error

    ok_response = _response(
        200,
        "application/json",
        json.dumps({
            "ok": True, "events_scanned": 3, "repaired": 1,
            "ignored": 2, "errors": 0, "attention": 0,
        }).encode(),
    )
    assert _report_response(ok_response) == 0
    assert "events_scanned=3" in capsys.readouterr().out
