import json

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
    result = vendor_publication.unpublish_vendor(company)
    assert result["published"] is False
    assert updates["public_webchat_enabled"] is False
