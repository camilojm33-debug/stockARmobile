"""Production readiness endpoint regression tests."""

def test_readyz_reports_database_readiness(app):
    response = app.test_client().get("/readyz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ready"}
