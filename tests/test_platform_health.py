from saas import _backup_health_status, _smtp_service_status


def test_backup_health_is_warning_before_first_monthly_cycle():
    status = _backup_health_status(0, 0)
    assert status["status"] == "warning"
    assert "política mensual" in status["detail"]


def test_backup_health_is_error_when_a_backup_failed():
    status = _backup_health_status(3, 1)
    assert status["status"] == "error"


def test_backup_health_is_ok_with_successful_backups():
    status = _backup_health_status(2, 0)
    assert status["status"] == "ok"


def test_smtp_health_is_warning_when_not_configured():
    status = _smtp_service_status(host="", user="", password="")
    assert status["status"] == "warning"
    assert "opcional" in status["detail"]


def test_smtp_health_rejects_partial_configuration():
    status = _smtp_service_status(host="smtp.example.com", user="user", password="")
    assert status["status"] == "error"


def test_smtp_health_is_ok_when_fully_configured():
    status = _smtp_service_status(host="smtp.example.com", user="user", password="secret")
    assert status["status"] == "ok"
