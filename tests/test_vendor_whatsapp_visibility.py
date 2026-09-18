from pathlib import Path

from flask import Flask

from stockarmobile.config import configure_app


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_FLAG = "WHATSAPP_VENDOR_UI_ENABLED"


def test_vendor_whatsapp_ui_is_hidden_by_default(monkeypatch):
    monkeypatch.delenv(UI_FLAG, raising=False)
    app = Flask(__name__)
    configure_app(app)
    assert app.config[UI_FLAG] is False


def test_vendor_whatsapp_ui_can_be_reenabled_by_environment(monkeypatch):
    monkeypatch.setenv(UI_FLAG, "1")
    app = Flask(__name__)
    configure_app(app)
    assert app.config[UI_FLAG] is True


def test_vendor_admin_templates_guard_whatsapp_ui():
    guard = '{% if config.get("WHATSAPP_VENDOR_UI_ENABLED", False) %}'
    for relative_path in (
        "templates/ai_agent/admin_v2.html",
        "templates/ai_agent/admin.html",
    ):
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert guard in content
        assert "WHATSAPP_VENDOR_UI_ENABLED" in content
