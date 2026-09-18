import json
import sys

from flask import Flask

from services.ai_agent import vendor_publication


def _company():
    company = type("Company", (), {})()
    company.id = 123
    company.active = True
    company.name = "Comercio Demo"
    company.preferences_json = json.dumps({"ai_agent": {}})
    return company


def test_publication_starts_unpublished():
    company = _company()
    status = vendor_publication._publication_from_company(company)
    assert status["slug"] == ""
    assert status["published"] is False


def test_new_slug_is_non_empty_and_url_safe():
    slug = vendor_publication._new_slug()
    assert slug
    assert "/" not in slug
    assert "_" not in slug


def test_regeneration_changes_slug(monkeypatch):
    company = _company()
    company.preferences_json = json.dumps(
        {
            "ai_agent": {
                "public_webchat_enabled": True,
                "public_vendor": {
                    "slug": "old-link",
                    "published": True,
                    "published_at": "2026-09-17T00:00:00+00:00",
                },
            }
        }
    )
    monkeypatch.setattr(vendor_publication, "_new_slug", lambda: "new-link")
    monkeypatch.setattr(vendor_publication, "_save_publication", lambda company, publication: publication)
    monkeypatch.setattr(vendor_publication, "update_ai_preferences", lambda company, **kwargs: None)
    monkeypatch.setattr(
        vendor_publication,
        "publication_status",
        lambda company: {"slug": "new-link", "published": True, "enabled": True, "available": True, "url": "https://example.test/vendedor/new-link"},
    )
    result = vendor_publication.regenerate_vendor_link(company)
    assert result["slug"] == "new-link"
    assert result["published"] is True


def test_unpublish_disables_public_webchat(monkeypatch):
    company = _company()
    company.preferences_json = json.dumps(
        {
            "ai_agent": {
                "public_webchat_enabled": True,
                "public_vendor": {"slug": "demo", "published": True},
            }
        }
    )
    monkeypatch.setattr(vendor_publication, "_save_publication", lambda company, publication: publication)
    updates = {}
    monkeypatch.setattr(
        vendor_publication,
        "update_ai_preferences",
        lambda company, **kwargs: updates.update(kwargs.get("ai_updates") or {}),
    )
    monkeypatch.setattr(
        vendor_publication,
        "publication_status",
        lambda company: {"slug": "demo", "published": False, "enabled": False, "available": False, "url": "https://example.test/vendedor/demo"},
    )
    result = vendor_publication.unpublish_vendor(company)
    assert result["published"] is False
    assert updates["public_webchat_enabled"] is False


def test_rate_limit_fails_closed_when_guard_unavailable(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setitem(sys.modules, "ai_agents", None)
    with app.app_context():
        assert vendor_publication._rate_limit(123) is False


def test_publication_page_resolves_company_from_authenticated_tenant(monkeypatch):
    from types import SimpleNamespace
    import app as stock_app

    company = SimpleNamespace(id=123, active=True, name="Comercio Demo")
    fake_query = SimpleNamespace(filter_by=lambda **kwargs: SimpleNamespace(first=lambda: company))
    monkeypatch.setattr(stock_app, "Company", SimpleNamespace(query=fake_query))
    monkeypatch.setattr(vendor_publication, "current_user", SimpleNamespace(company_id=123))
    monkeypatch.setattr(vendor_publication, "get_current_company_id", lambda user: user.company_id)
    monkeypatch.setattr(
        vendor_publication,
        "publication_status",
        lambda value: {"slug": "", "published": False, "enabled": False, "available": False, "url": None},
    )
    monkeypatch.setattr(vendor_publication, "can_use_ai", lambda company, agent: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr(vendor_publication, "url_for", lambda *args, **kwargs: "/preview")
    monkeypatch.setattr(vendor_publication, "qr_data_uri", lambda value: "qr")
    monkeypatch.setattr(
        vendor_publication,
        "render_template",
        lambda template, **context: {"template": template, "company": context["company"]},
    )

    with stock_app.app.test_request_context("/agentes-ia/vendedor/publicacion"):
        result = vendor_publication.publication_page.__wrapped__()

    assert result["template"] == "ai_agents/vendor_publication.html"
    assert result["company"] is company


def test_special_agent_options_have_safe_defaults_and_marketing_approval():
    from services.ai_agent.config_service import get_special_options, normalize_special_options

    company = _company()
    analyst = get_special_options(company, "analista")
    marketing = get_special_options(company, "marketing")

    assert analyst["default_period"] == "30d"
    assert "critical_stock" in analyst["alerts"]
    assert analyst["output_style"] == "accionable"
    assert marketing["default_segment"] == "inactivos"
    assert marketing["approval_required"] is True

    normalized = normalize_special_options(
        "analista",
        {"default_period": "999d", "alerts": ["critical_stock", "unexpected"], "output_style": "invalid"},
    )
    assert normalized["default_period"] == "30d"
    assert normalized["alerts"] == ["critical_stock", "unexpected"]
    assert normalized["output_style"] == "accionable"
