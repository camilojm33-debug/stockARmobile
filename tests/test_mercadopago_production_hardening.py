from urllib.parse import parse_qs, urlparse

import requests

from services.mercadopago_oauth_service import MercadoPagoOAuthService


def test_oauth_authorization_requests_offline_access(monkeypatch):
    monkeypatch.setenv("MP_CLIENT_ID", "client-test")
    monkeypatch.delenv("MP_OAUTH_SCOPE", raising=False)

    service = MercadoPagoOAuthService()
    url = service.build_authorization_url(state="state", redirect_uri="https://www.stockarmobile.com/admin/mercado-pago/callback")

    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["client-test"]
    assert "offline_access" in query["scope"][0].split()


def test_oauth_authorization_preserves_custom_scopes_and_adds_offline_access(monkeypatch):
    monkeypatch.setenv("MP_CLIENT_ID", "client-test")
    monkeypatch.setenv("MP_OAUTH_SCOPE", "read write")

    service = MercadoPagoOAuthService()
    url = service.build_authorization_url(state="state", redirect_uri="https://example.test/callback")

    scopes = parse_qs(urlparse(url).query)["scope"][0].split()
    assert scopes[:2] == ["read", "write"]
    assert scopes.count("offline_access") == 1


def test_oauth_refresh_errors_treat_rate_limit_and_server_errors_as_transient():
    assert MercadoPagoOAuthService._is_transient_refresh_error(RuntimeError("Mercado Pago OAuth error 429: busy")) is True
    assert MercadoPagoOAuthService._is_transient_refresh_error(RuntimeError("Mercado Pago OAuth error 500: unavailable")) is True
    assert MercadoPagoOAuthService._is_transient_refresh_error(RuntimeError("Mercado Pago OAuth error 401: invalid refresh token")) is False


def test_oauth_network_errors_are_transient():
    exc = requests.Timeout("timed out")
    assert MercadoPagoOAuthService._is_transient_refresh_error(exc) is True
