from __future__ import annotations

import pytest

from services.ai_agent.whatsapp_campaign_template_service import get_campaign_template


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


def _compatible_template(name="stockarmobile_marketing", language="es_AR"):
    return {
        "name": name,
        "language": language,
        "status": "APPROVED",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": "Hola {{1}}, te contamos sobre {{2}}. {{3}}",
            }
        ],
    }


def _connection(**updates):
    return {
        "enabled": True,
        "phone_number_id": "platform-phone-id",
        "business_account_id": "platform-waba-id",
        "access_token": "test-token",
        "template_name": "",
        "template_language": "es_AR",
        **updates,
    }


def test_campaign_template_auto_selects_only_one_compatible_approved_template(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": [_compatible_template()]}),
    )

    template = get_campaign_template(_connection())

    assert template["name"] == "stockarmobile_marketing"
    assert template["language"] == "es_AR"
    assert template["status"] == "APPROVED"
    assert template["campaign_compatible"] is True
    assert template["auto_selected"] is True


def test_campaign_template_requires_superadmin_default_when_multiple_are_compatible(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({
            "data": [
                _compatible_template("marketing_a"),
                _compatible_template("marketing_b"),
            ]
        }),
    )

    with pytest.raises(ValueError, match="2 plantillas aprobadas compatibles"):
        get_campaign_template(_connection())


def test_campaign_template_uses_explicitly_configured_name_and_language(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({
            "data": [
                _compatible_template("unused"),
                _compatible_template("selected", "es_AR"),
            ]
        }),
    )

    template = get_campaign_template(
        _connection(template_name="selected", template_language="es_AR")
    )

    assert template["name"] == "selected"
    assert template["auto_selected"] is False


def test_campaign_template_explains_when_selected_name_is_not_approved(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": [_compatible_template()]}),
    )

    with pytest.raises(ValueError, match="no figura aprobada"):
        get_campaign_template(_connection(template_name="old_template"))


def test_campaign_template_rejects_utility_templates_for_marketing(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    utility = _compatible_template()
    utility["category"] = "UTILITY"
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": [utility]}),
    )

    with pytest.raises(ValueError, match="No hay una plantilla aprobada compatible"):
        get_campaign_template(_connection())


def test_campaign_template_rejects_media_header_until_sender_supports_it(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    media = _compatible_template()
    media["components"].insert(0, {"type": "HEADER", "format": "IMAGE"})
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": [media]}),
    )

    with pytest.raises(ValueError, match="encabezado multimedia"):
        get_campaign_template(_connection(template_name="stockarmobile_marketing"))


def test_campaign_template_reports_missing_waba_and_token_separately():
    with pytest.raises(ValueError, match="Falta el WABA ID"):
        get_campaign_template(_connection(business_account_id=""))

    with pytest.raises(ValueError, match="Falta el token de acceso"):
        get_campaign_template(_connection(access_token=""))


def test_campaign_template_follows_meta_pagination_and_bounds_pages(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse({
                "data": [],
                "paging": {"next": "https://graph.facebook.com/v26.0/waba/message_templates?after=page2"},
            })
        return FakeResponse({"data": [_compatible_template()]})

    monkeypatch.setattr(service.requests, "get", fake_get)

    template = get_campaign_template(_connection())

    assert template["name"] == "stockarmobile_marketing"
    assert len(calls) == 2


def test_campaign_template_rejects_untrusted_meta_pagination_host(monkeypatch):
    from services.ai_agent import whatsapp_campaign_template_service as service

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({
            "data": [],
            "paging": {"next": "https://attacker.invalid/steal"},
        }),
    )

    with pytest.raises(ValueError, match="dirección de paginación no válida"):
        get_campaign_template(_connection())
