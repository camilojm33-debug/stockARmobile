import importlib

import pytest


@pytest.fixture
def health_client():
    import app as stock_app

    stock_app.app.config["TESTING"] = True
    import wsgi
    importlib.reload(wsgi)
    with stock_app.app.test_client() as client:
        yield client


def test_health_endpoint_returns_ok_when_database_is_available(health_client):
    response = health_client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
