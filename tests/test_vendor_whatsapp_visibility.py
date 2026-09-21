import re
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


def test_vendor_admin_templates_keep_whatsapp_out_of_general_v2_settings():
    guard = '{% if config.get("WHATSAPP_VENDOR_UI_ENABLED", False) %}'
    admin_v2 = (REPO_ROOT / "templates/ai_agent/admin_v2.html").read_text(encoding="utf-8")
    assert guard not in admin_v2
    assert "id=\"whatsapp-vendedor\"" not in admin_v2
    assert "name=\"whatsapp_enabled\"" not in admin_v2


def test_legacy_admin_template_still_guards_whatsapp_ui():
    guard = '{% if config.get("WHATSAPP_VENDOR_UI_ENABLED", False) %}'
    content = (REPO_ROOT / "templates/ai_agent/admin.html").read_text(encoding="utf-8")
    assert guard in content
    assert "WHATSAPP_VENDOR_UI_ENABLED" in content


def test_vendor_page_whatsapp_ui_is_feature_flagged():
    content = (REPO_ROOT / "templates/ai_agents/index.html").read_text(encoding="utf-8")
    guarded_block = '{% if config.get("WHATSAPP_VENDOR_UI_ENABLED", False) %}\n  <div class="col-lg-5"'
    assert guarded_block in content
    assert "vendor_whatsapp_connect" in content


def test_vendor_whatsapp_uses_tenant_safe_company_access():
    ai_agents_content = (REPO_ROOT / "ai_agents.py").read_text(encoding="utf-8")
    connect_start = ai_agents_content.index("def vendor_whatsapp_connect():")
    complete_start = ai_agents_content.index("def vendor_whatsapp_complete():")
    public_start = ai_agents_content.index('@bp.get("/public/vendedor/<token>")')
    connect_block = ai_agents_content[connect_start:complete_start]
    complete_block = ai_agents_content[complete_start:public_start]
    assert "can_use_ai_feature(current_user.company, " not in connect_block
    assert "can_use_ai_feature(current_user.company, " not in complete_block
    assert "_current_active_company()" in connect_block
    assert "_current_active_company()" in complete_block
    assert "Company.query.filter_by(id=current_user.company_id, active=True).first()" in ai_agents_content

    admin_content = (REPO_ROOT / "services/ai_agent/admin.py").read_text(encoding="utf-8")
    start = admin_content.index("def vendor_metrics():")
    end = admin_content.index("\n\n@bp.", start)
    vendor_metrics_block = admin_content[start:end]
    assert "can_use_ai_feature(current_user.company, " not in vendor_metrics_block
    assert "get_current_company_id(current_user)" in vendor_metrics_block
    assert 'can_use_ai_feature(company, "vendedor")' in vendor_metrics_block


def test_whatsapp_webhook_has_a_scoped_csrf_exemption():
    app_content = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    webhook_content = (REPO_ROOT / "whatsapp_agent.py").read_text(encoding="utf-8")

    assert "csrf.exempt(whatsapp_agent_bp)" not in app_content
    route = '@bp.route("/api/whatsapp/webhook", methods=["GET", "POST"])'
    assert route in webhook_content
    route_start = webhook_content.index(route)
    webhook_start = webhook_content.index("def webhook()", route_start)
    scoped_block = webhook_content[route_start:webhook_start]
    assert "@csrf.exempt" in scoped_block


def test_ai_agent_routes_do_not_use_broken_user_company_relationship():
    content = (REPO_ROOT / "ai_agents.py").read_text(encoding="utf-8")
    assert re.search(r"current_user\\.company(?!_)", content) is None
    assert "can_use_ai_feature(_current_active_company(), \"marketing\")" in content
    assert "company = _current_active_company()" in content
