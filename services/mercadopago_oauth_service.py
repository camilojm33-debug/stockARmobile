"""Servicio OAuth de Mercado Pago por empresa."""

from __future__ import annotations

import base64
import hashlib
import logging
import json
import os
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet
from flask import current_app, has_app_context

from app import MercadoPagoConnection, db, utcnow


class MercadoPagoOAuthService:
    AUTH_URL = "https://auth.mercadopago.com.ar/authorization"
    TOKEN_URL = "https://api.mercadopago.com/oauth/token"
    USERINFO_URL = "https://api.mercadopago.com/users/me"

    def __init__(self):
        self.session = requests.Session()

    def _logger(self):
        if has_app_context():
            return current_app.logger
        return logging.getLogger(__name__)

    def _client_id(self) -> str:
        return (os.environ.get("MP_CLIENT_ID") or "").strip()

    def _client_secret(self) -> str:
        return (os.environ.get("MP_CLIENT_SECRET") or "").strip()

    def oauth_config_status(self) -> tuple[bool, list[str]]:
        missing = []
        if not self._client_id():
            missing.append("MP_CLIENT_ID")
        if not self._client_secret():
            missing.append("MP_CLIENT_SECRET")
        if not (os.environ.get("MP_OAUTH_ENCRYPTION_KEY") or "").strip():
            missing.append("MP_OAUTH_ENCRYPTION_KEY")
        return (len(missing) == 0, missing)

    def _redirect_uri(self) -> str:
        return (os.environ.get("MP_OAUTH_REDIRECT_URI") or "").strip()

    def has_oauth_config(self) -> bool:
        ok, _missing = self.oauth_config_status()
        return ok

    def oauth_redirect_uri(self, fallback: str) -> str:
        return self._redirect_uri() or fallback

    def default_oauth_redirect_uri(self) -> str:
        app_url = ""
        if has_app_context():
            app_url = (current_app.config.get("APP_URL") or "").strip().rstrip("/")
        if not app_url:
            app_url = (os.environ.get("APP_URL") or "https://www.stockarmobile.com").strip().rstrip("/")
        return f"{app_url}/admin/mercado-pago/callback"

    def build_authorization_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._client_id(),
            "response_type": "code",
            "platform_id": "mp",
            "state": state,
            "redirect_uri": redirect_uri,
        }
        authorization_url = f"{self.AUTH_URL}?{urlencode(params)}"
        self._logger().info(
            "Mercado Pago OAuth authorization generated: url=%s client_id=%s redirect_uri=%s state=%s",
            authorization_url,
            params["client_id"],
            redirect_uri,
            state,
        )
        return authorization_url

    def _post_token(self, *, payload: dict[str, str]) -> dict:
        client_id = self._client_id()
        client_secret = self._client_secret()
        if not client_id or not client_secret:
            raise RuntimeError("MP_CLIENT_ID/MP_CLIENT_SECRET no configurados")
        body = {
            "client_id": client_id,
            "client_secret": client_secret,
            **payload,
        }
        self._logger().info(
            "Mercado Pago OAuth token request: url=%s client_id=%s grant_type=%s redirect_uri=%s has_code=%s has_refresh_token=%s",
            self.TOKEN_URL,
            client_id,
            payload.get("grant_type"),
            payload.get("redirect_uri"),
            bool(payload.get("code")),
            bool(payload.get("refresh_token")),
        )
        response = self.session.post(self.TOKEN_URL, data=body, timeout=25)
        if response.status_code >= 400:
            self._logger().error(
                "Mercado Pago OAuth token error: http_status=%s response_body=%s",
                response.status_code,
                response.text,
            )
            raise RuntimeError(f"Mercado Pago OAuth error {response.status_code}: {response.text[:500]}")
        token_payload = response.json()
        self._logger().info(
            "Mercado Pago OAuth token response received: http_status=%s keys=%s",
            response.status_code,
            sorted(token_payload.keys()),
        )
        return token_payload

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        return self._post_token(
            payload={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )

    def refresh_tokens(self, *, refresh_token: str) -> dict:
        return self._post_token(
            payload={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    def fetch_user_profile(self, *, access_token: str) -> dict:
        response = self.session.get(
            self.USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=25,
        )
        if response.status_code >= 400:
            self._logger().error(
                "Mercado Pago OAuth userinfo error: http_status=%s response_body=%s",
                response.status_code,
                response.text,
            )
            raise RuntimeError(f"Mercado Pago user profile error {response.status_code}: {response.text[:500]}")
        profile = response.json()
        self._logger().info(
            "Mercado Pago OAuth userinfo response received: http_status=%s keys=%s",
            response.status_code,
            sorted(profile.keys()),
        )
        return profile

    def _fernet(self) -> Fernet:
        raw_env_key = (os.environ.get("MP_OAUTH_ENCRYPTION_KEY") or "").strip()
        if not raw_env_key:
            raise RuntimeError("MP_OAUTH_ENCRYPTION_KEY no configurada")
        raw_key = raw_env_key.encode("utf-8")
        derived = hashlib.sha256(raw_key).digest()
        return Fernet(base64.urlsafe_b64encode(derived))

    def encrypt_value(self, value: str | None) -> str | None:
        if not value:
            return None
        return self._fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt_value(self, value: str | None) -> str | None:
        if not value:
            return None
        return self._fernet().decrypt(value.encode("utf-8")).decode("utf-8")

    def get_connection(self, company_id: int) -> MercadoPagoConnection | None:
        return MercadoPagoConnection.query.filter_by(company_id=company_id).first()

    def summarize_connection(self, connection: MercadoPagoConnection | None) -> dict:
        if connection is None:
            return {
                "connected": False,
                "status": "disconnected",
                "status_label": "No conectado",
                "account_name": None,
                "account_email": None,
                "country": None,
                "connected_at": None,
                "last_synced_at": None,
                "token_expires_at": None,
            }
        return {
            "connected": (connection.status or "disconnected") == "connected",
            "status": connection.status or "disconnected",
            "status_label": "Cuenta conectada" if (connection.status or "disconnected") == "connected" else "No conectado",
            "account_name": connection.account_name,
            "account_email": connection.account_email,
            "country": connection.country,
            "connected_at": connection.connected_at,
            "last_synced_at": connection.last_synced_at,
            "token_expires_at": connection.token_expires_at,
            "mp_user_id": connection.mp_user_id,
        }

    def save_connection(self, *, company_id: int, token_payload: dict, profile: dict) -> MercadoPagoConnection:
        connection = self.get_connection(company_id)
        if connection is None:
            connection = MercadoPagoConnection(company_id=company_id)
            db.session.add(connection)
        now = utcnow()
        expires_in = int(token_payload.get("expires_in") or 0)
        connection.mp_user_id = str(profile.get("id") or connection.mp_user_id or "")
        connection.account_name = str(profile.get("first_name") or profile.get("nickname") or profile.get("username") or profile.get("id") or "").strip()[:160] or None
        connection.account_email = (profile.get("email") or connection.account_email or "").strip()[:160] or None
        connection.country = (profile.get("country_id") or profile.get("country") or connection.country or "").strip()[:80] or None
        connection.status = "connected"
        connection.connected_at = connection.connected_at or now
        connection.last_synced_at = now
        connection.token_expires_at = now + timedelta(seconds=expires_in) if expires_in else None
        connection.access_token_encrypted = self.encrypt_value(token_payload.get("access_token"))
        connection.refresh_token_encrypted = self.encrypt_value(token_payload.get("refresh_token"))
        connection.scope = (token_payload.get("scope") or connection.scope or "").strip()[:255] or None
        connection.metadata_json = json.dumps({
            "user_profile": profile,
            "token_type": token_payload.get("token_type"),
        }, ensure_ascii=False)
        db.session.flush()
        return connection

    def _refresh_if_needed(self, connection: MercadoPagoConnection) -> MercadoPagoConnection:
        now = utcnow()
        if connection.token_expires_at and connection.token_expires_at > now + timedelta(minutes=5):
            return connection
        refresh_token = self.decrypt_value(connection.refresh_token_encrypted)
        if not refresh_token:
            connection.status = "disconnected"
            connection.access_token_encrypted = None
            connection.refresh_token_encrypted = None
            connection.token_expires_at = None
            connection.last_synced_at = now
            db.session.commit()
            raise RuntimeError("Mercado Pago requiere una nueva autorización")
        try:
            token_payload = self.refresh_tokens(refresh_token=refresh_token)
            profile = self.fetch_user_profile(access_token=token_payload.get("access_token") or "")
        except Exception as exc:
            connection.status = "disconnected"
            connection.access_token_encrypted = None
            connection.refresh_token_encrypted = None
            connection.token_expires_at = None
            connection.last_synced_at = now
            db.session.commit()
            raise RuntimeError("Mercado Pago requiere una nueva autorización") from exc
        return self.save_connection(company_id=connection.company_id, token_payload=token_payload, profile=profile)

    def ensure_access_token(self, *, company_id: int) -> str:
        connection = self.get_connection(company_id)
        if connection is None or (connection.status or "disconnected") != "connected":
            raise RuntimeError("Mercado Pago no está conectado para esta empresa")
        connection = self._refresh_if_needed(connection)
        access_token = self.decrypt_value(connection.access_token_encrypted)
        if not access_token:
            raise RuntimeError("No se pudo recuperar el access token de Mercado Pago")
        return access_token

    def refresh_connection(self, *, company_id: int) -> MercadoPagoConnection:
        connection = self.get_connection(company_id)
        if connection is None or (connection.status or "disconnected") != "connected":
            raise RuntimeError("Mercado Pago no está conectado para esta empresa")
        return self._refresh_if_needed(connection)

    def test_connection(self, *, company_id: int) -> dict:
        connection = self.refresh_connection(company_id=company_id)
        access_token = self.decrypt_value(connection.access_token_encrypted)
        if not access_token:
            raise RuntimeError("No se pudo obtener un access token válido")
        profile = self.fetch_user_profile(access_token=access_token)
        connection.last_synced_at = utcnow()
        connection.account_name = str(profile.get("first_name") or profile.get("nickname") or profile.get("username") or profile.get("id") or connection.account_name or "").strip()[:160] or connection.account_name
        connection.account_email = (profile.get("email") or connection.account_email or "").strip()[:160] or None
        connection.country = (profile.get("country_id") or profile.get("country") or connection.country or "").strip()[:80] or None
        db.session.commit()
        return profile

    def disconnect(self, *, company_id: int) -> MercadoPagoConnection:
        connection = self.get_connection(company_id)
        if connection is None:
            raise RuntimeError("No existe una conexión de Mercado Pago para desconectar")
        connection.status = "disconnected"
        connection.access_token_encrypted = None
        connection.refresh_token_encrypted = None
        connection.token_expires_at = None
        connection.last_synced_at = utcnow()
        connection.metadata_json = json.dumps({"disconnected_at": connection.last_synced_at.isoformat() if connection.last_synced_at else None}, ensure_ascii=False)
        db.session.commit()
        return connection

    @staticmethod
    def oauth_state() -> str:
        return secrets.token_urlsafe(24)
