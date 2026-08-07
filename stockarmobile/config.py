"""Application configuration and runtime flags."""

import os
import sys

from werkzeug.middleware.proxy_fix import ProxyFix


def runtime_flags():
    is_production_env = os.environ.get("FLASK_ENV") == "production" or bool(os.environ.get("RENDER"))
    is_pytest_context = "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    return is_production_env, is_pytest_context


def configure_app(app):
    """Populate Flask config from environment while preserving legacy defaults."""
    is_production_env, is_pytest_context = runtime_flags()
    secret_key = os.environ.get("SECRET_KEY")
    if is_production_env and not is_pytest_context and not secret_key:
        raise RuntimeError("SECRET_KEY es obligatorio en produccion.")

    app.config["IS_PRODUCTION_ENV"] = is_production_env
    app.config["IS_PYTEST_CONTEXT"] = is_pytest_context

    app.config["SECRET_KEY"] = secret_key or "stockarmobile-dev-secret"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    if is_production_env:
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["REMEMBER_COOKIE_SECURE"] = True
        app.config["PREFERRED_URL_SCHEME"] = "https"

    database_url = os.environ.get("DATABASE_URL", "sqlite:///stock_armobile.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.config["SUPPORT_EMAIL"] = (os.environ.get("SUPPORT_EMAIL") or os.environ.get("LANDING_EMAIL") or "stockarmobile@gmail.com").strip()
    app.config["SUPPORT_WHATSAPP_DISPLAY"] = (os.environ.get("SUPPORT_WHATSAPP_DISPLAY") or os.environ.get("LANDING_WHATSAPP") or "+54 9 3624 22-8296").strip()
    app.config["SUPPORT_WHATSAPP_NUMBER"] = (
        os.environ.get("SUPPORT_WHATSAPP_NUMBER")
        or "".join(ch for ch in app.config["SUPPORT_WHATSAPP_DISPLAY"] if ch.isdigit())
        or "5493624228296"
    ).strip()
    app.config["PASSWORD_RESET_TOKEN_TTL_MINUTES"] = int(os.environ.get("PASSWORD_RESET_TOKEN_TTL_MINUTES", "60"))
    app.config["SMTP_HOST"] = (os.environ.get("SMTP_HOST") or "").strip()
    app.config["SMTP_PORT"] = int(os.environ.get("SMTP_PORT") or "587")
    app.config["SMTP_USE_TLS"] = (os.environ.get("SMTP_USE_TLS") or "1").strip().lower() in {"1", "true", "yes", "on"}
    app.config["SMTP_USER"] = (os.environ.get("SMTP_USER") or "").strip()
    app.config["SMTP_PASSWORD"] = (os.environ.get("SMTP_PASSWORD") or "").strip()
    app.config["SMTP_FROM_EMAIL"] = (os.environ.get("SMTP_FROM_EMAIL") or app.config["SUPPORT_EMAIL"] or "no-reply@stockarmobile.com").strip()
    app.config["APP_URL"] = (os.environ.get("APP_URL") or "https://www.stockarmobile.com").strip().rstrip("/")
    app.config["COMPANY_PIN_SESSION_TTL_MINUTES"] = int(os.environ.get("COMPANY_PIN_SESSION_TTL_MINUTES", "30"))

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
