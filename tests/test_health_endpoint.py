from unittest.mock import patch


def test_health_endpoint_returns_ok_when_database_is_available(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_health_endpoint_returns_503_when_database_probe_fails(client):
    with patch("wsgi.db.session.execute", side_effect=RuntimeError("db unavailable")):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.get_json() == {"status": "error"}
