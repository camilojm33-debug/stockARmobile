from flask import Flask

import pytest

from stockarmobile.config import configure_app


def _configure(monkeypatch, database_url="postgresql+psycopg://test:test@localhost/stockarmobile_test"):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SECRET_KEY", "configuration-test-secret")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    app = Flask("database-pool-test")
    configure_app(app)
    return app


def test_postgresql_pool_defaults_preserve_existing_sqlalchemy_limits(monkeypatch):
    app = _configure(monkeypatch)

    options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    assert options["pool_pre_ping"] is True
    assert options["pool_recycle"] == 1800
    assert options["pool_size"] == 5
    assert options["max_overflow"] == 10
    assert options["pool_timeout"] == 30


def test_postgresql_pool_can_be_tuned_with_bounded_environment_values(monkeypatch):
    app = _configure(monkeypatch)
    monkeypatch.setenv("DB_POOL_SIZE", "8")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "45")

    configure_app(app)
    options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    assert options["pool_size"] == 8
    assert options["max_overflow"] == 4
    assert options["pool_timeout"] == 45


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_POOL_SIZE", "0"),
        ("DB_POOL_SIZE", "26"),
        ("DB_MAX_OVERFLOW", "-1"),
        ("DB_MAX_OVERFLOW", "26"),
        ("DB_POOL_TIMEOUT_SECONDS", "0"),
        ("DB_POOL_TIMEOUT_SECONDS", "121"),
        ("DB_POOL_SIZE", "five"),
    ],
)
def test_invalid_postgresql_pool_settings_fail_fast(monkeypatch, name, value):
    app = _configure(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        configure_app(app)


def test_sqlite_configuration_does_not_receive_postgresql_pool_options(monkeypatch):
    app = _configure(monkeypatch, "sqlite:///stockarmobile-test.db")

    options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    assert options["connect_args"]["check_same_thread"] is False
    assert "pool_size" not in options
    assert "max_overflow" not in options
