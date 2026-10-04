from pathlib import Path


def test_direct_agent_views_have_ai_error_helper_outside_dashboard_block():
    template = Path(__file__).resolve().parents[1] / "templates" / "ai_agents" / "index.html"
    source = template.read_text(encoding="utf-8")

    extra_js = source.split("{% block extra_js %}", 1)[1]
    helper_pos = extra_js.find("window.publicAiError")
    dashboard_pos = extra_js.find("{% if view == 'dashboard' and any_chat_agent %}")
    special_pos = extra_js.find("{% if view in ['analista', 'marketing'] %}")

    assert helper_pos >= 0
    assert dashboard_pos >= 0
    assert special_pos >= 0
    assert helper_pos < dashboard_pos
    assert helper_pos < special_pos
    assert "throw new Error(publicAiError(data.error" in extra_js
