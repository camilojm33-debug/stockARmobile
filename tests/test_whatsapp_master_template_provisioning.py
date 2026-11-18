from __future__ import annotations

from services.ai_agent import whatsapp_master_template_service as service

# Re-run all checks against the restored main base before production deployment.


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


def _connection(**updates):
    return {
        "enabled": True,
        "phone_number_id": "merchant-phone-id",
        "business_account_id": "987654321",
        "access_token": "merchant-access-token",
        **updates,
    }


def _existing_master(status="APPROVED", *, master=None, body=None, category="MARKETING"):
    master = master or service.MASTER_TEMPLATES[0]
    return {
        "id": f"template-{master['key']}",
        "name": master["name"],
        "language": master["language"],
        "status": status,
        "category": category,
        "components": [
            {
                "type": "BODY",
                "text": body if body is not None else master["body_text"],
            }
        ],
    }


def _existing_all_masters(status="APPROVED"):
    return [
        _existing_master(status, master=master)
        for master in service.MASTER_TEMPLATES
    ]


def test_provision_creates_missing_master_template_for_connected_waba(monkeypatch):
    calls = {"get": [], "post": []}

    def fake_get(url, **kwargs):
        calls["get"].append((url, kwargs))
        return FakeResponse({"data": []})

    def fake_post(url, **kwargs):
        calls["post"].append((url, kwargs))
        return FakeResponse({"id": "created-456", "status": "PENDING"})

    monkeypatch.setattr(service.requests, "get", fake_get)
    monkeypatch.setattr(service.requests, "post", fake_post)

    results = service.provision_master_templates(_connection())

    assert len(results) == len(service.MASTER_TEMPLATES) == 4
    assert [item["key"] for item in results] == [item["key"] for item in service.MASTER_TEMPLATES]
    assert all(item["status"] == "PENDING" and item["created"] for item in results)
    assert calls["get"][0][0].endswith("/987654321/message_templates")
    assert len(calls["post"]) == len(service.MASTER_TEMPLATES)
    assert all(call[0].endswith("/987654321/message_templates") for call in calls["post"])

    payload = calls["post"][0][1]["json"]
    assert payload["category"] == "MARKETING"
    assert payload["language"] == "es_AR"
    assert payload["components"][0]["type"] == "BODY"
    assert payload["components"][0]["text"] == service.MASTER_TEMPLATES[0]["body_text"]
    assert payload["components"][0]["example"]["body_text"] == [
        service.MASTER_TEMPLATES[0]["examples"]
    ]


def test_provision_is_idempotent_when_matching_master_is_already_approved(monkeypatch):
    post_calls = []
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": _existing_all_masters("APPROVED")}),
    )
    monkeypatch.setattr(
        service.requests,
        "post",
        lambda *args, **kwargs: post_calls.append(args) or FakeResponse({}),
    )

    results = service.provision_master_templates(_connection())

    assert len(results) == len(service.MASTER_TEMPLATES)
    assert all(item["status"] == "APPROVED" and item["created"] is False for item in results)
    assert "lista" in results[0]["message"]
    assert post_calls == []


def test_provision_reports_pending_master_without_creating_duplicate(monkeypatch):
    post_calls = []
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": _existing_all_masters("PENDING")}),
    )
    monkeypatch.setattr(
        service.requests,
        "post",
        lambda *args, **kwargs: post_calls.append(args) or FakeResponse({}),
    )

    results = service.provision_master_templates(_connection())

    assert len(results) == len(service.MASTER_TEMPLATES)
    assert all(item["status"] == "PENDING" and item["created"] is False for item in results)
    assert "pendiente" in results[0]["message"]
    assert post_calls == []


def test_provision_does_not_overwrite_same_name_with_different_content(monkeypatch):
    post_calls = []
    existing = _existing_master(body="Hola {{1}}, este no es el contenido maestro {{2}} {{3}}")
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"data": [existing]}),
    )
    monkeypatch.setattr(
        service.requests,
        "post",
        lambda *args, **kwargs: post_calls.append(args) or FakeResponse({}),
    )

    results = service.provision_master_templates(_connection())

    assert results[0]["status"] == "CONFLICT"
    assert "No se modificó" in results[0]["message"]
    assert len(post_calls) == len(service.MASTER_TEMPLATES) - 1


def test_provision_reports_missing_meta_permission_without_exposing_token(monkeypatch):
    get_calls = []

    def fake_get(*args, **kwargs):
        get_calls.append(args)
        return FakeResponse({"data": []})

    monkeypatch.setattr(service.requests, "get", fake_get)
    monkeypatch.setattr(
        service.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {"error": {"code": 200, "message": "Permissions error"}},
            status_code=400,
        ),
    )

    results = service.provision_master_templates(_connection())

    assert len(get_calls) == len(service.MASTER_TEMPLATES) + 1  # Initial read plus a safe race check for each template.
    assert len(results) == len(service.MASTER_TEMPLATES)
    assert all(item["status"] == "ERROR" for item in results)
    assert "whatsapp_business_management" in results[0]["message"]
    assert "merchant-access-token" not in results[0]["message"]


def test_provision_rejects_invalid_or_incomplete_connection_before_meta_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: calls.append(args) or FakeResponse({"data": []}),
    )
    monkeypatch.setattr(
        service.requests,
        "post",
        lambda *args, **kwargs: calls.append(args) or FakeResponse({}),
    )

    for updates, expected in [
        ({"enabled": False}, "deshabilitado"),
        ({"business_account_id": "not-a-waba-id"}, "WABA ID"),
        ({"access_token": ""}, "autorización"),
        ({"phone_number_id": ""}, "Phone Number ID"),
    ]:
        try:
            service.provision_master_templates(_connection(**updates))
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("Expected connection validation to fail.")

    assert calls == []


def test_master_catalog_exposes_only_non_secret_template_metadata():
    catalog = service.list_master_templates()

    assert len(catalog) == 4
    by_goal = {item["goal_key"]: item for item in catalog}
    assert set(by_goal) == {
        "customer_marketing",
        "customer_reactivation",
        "customer_loyalty",
        "customer_new_product",
    }
    assert by_goal["customer_marketing"]["name"] == "campana_marketing_es_ar"
    assert by_goal["customer_reactivation"]["name"] == "campana_reactivacion_es_ar"
    assert by_goal["customer_loyalty"]["name"] == "campana_fidelizacion_es_ar"
    assert by_goal["customer_new_product"]["name"] == "campana_novedad_producto_es_ar"
    for item in catalog:
        assert item["category"] == "MARKETING"
        assert "{{1}}" in item["body_text"]
        assert "{{2}}" in item["body_text"]
        assert "{{3}}" in item["body_text"]
        assert len(item["default_parameter_fields"]) == 3
        assert "access_token" not in item


def test_onboarding_and_tenant_ui_use_idempotent_master_provisioning():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "ai_agents.py").read_text(encoding="utf-8")
    template = (root / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")
    superadmin_source = (root / "saas.py").read_text(encoding="utf-8")
    superadmin_template = (root / "templates/saas/whatsapp_master_templates.html").read_text(encoding="utf-8")

    assert "provision_master_templates(connection)" in source
    assert '@bp.post("/vendedor/whatsapp/plantillas/predeterminadas")' in source
    assert "@tenant_required" in source
    assert "@company_admin_required" in source
    assert "template_provision_url=url_for" in source
    assert "Instalar / sincronizar" in template
    assert "fetch(provisionUrl" in template
    assert "Meta revisa cada copia por separado" in template
    assert "Plantillas aprobadas disponibles" in template
    assert "Solo se muestran plantillas que Meta ya aprobó" in template
    assert "Estado de la plantilla maestra" in template
    assert "La plantilla general está pendiente de aprobación" in template
    assert "Plantilla aprobada</label>" not in template
    assert '@bp.get("/whatsapp-plantillas-maestras")' in superadmin_source
    assert "list_master_templates()" in superadmin_source
    assert "Biblioteca de plantillas maestras" in superadmin_template



def test_provisioning_state_is_saved_without_tokens_for_default_company_and_branch():
    import json
    from types import SimpleNamespace

    company = SimpleNamespace(preferences_json="")
    results = [{
        "key": "marketing_general",
        "name": "campana_marketing_es_ar",
        "language": "es_AR",
        "status": "PENDING",
        "created": True,
        "message": "Pendiente de aprobación",
        "access_token": "must-not-be-saved",
    }]

    company_state = service.record_master_template_provisioning(company, results)
    company_payload = json.loads(company.preferences_json)
    stored_company = company_payload["ai_agent"]["whatsapp"]["master_template_sync"]
    assert company_state["templates"][0]["status"] == "PENDING"
    assert stored_company["templates"][0]["name"] == "campana_marketing_es_ar"
    assert "access_token" not in json.dumps(stored_company)

    branch = SimpleNamespace(is_default=False, vendor_ai_json="")
    branch_state = service.record_master_template_provisioning(company, results, branch=branch)
    branch_payload = json.loads(branch.vendor_ai_json)
    stored_branch = branch_payload["whatsapp"]["master_template_sync"]
    assert branch_state["templates"][0]["status"] == "PENDING"
    assert stored_branch["templates"][0]["language"] == "es_AR"
    assert "must-not-be-saved" not in branch.vendor_ai_json
