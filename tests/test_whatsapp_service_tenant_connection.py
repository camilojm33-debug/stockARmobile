from types import SimpleNamespace

from services.ai_agent.whatsapp_service import WhatsAppService


class _FakeResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"messages": [{"id": "wamid.tenant-owned-phone"}]}


def test_template_send_uses_exact_resolved_tenant_phone_and_token(monkeypatch):
    calls = []
    connection = {
        "enabled": True,
        "phone_number_id": "TENANT_PHONE_NUMBER_ID",
        "business_account_id": "TENANT_WABA_ID",
        "access_token": "tenant-specific-meta-token",
    }

    def fake_post(url, *, json, headers, timeout):
        calls.append({
            "url": url,
            "payload": json,
            "headers": headers,
            "timeout": timeout,
        })
        return _FakeResponse()

    monkeypatch.setattr(
        "services.ai_agent.whatsapp_service.requests.post",
        fake_post,
    )
    monkeypatch.setattr(
        "services.ai_agent.whatsapp_service.get_whatsapp_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("An explicitly resolved sender connection must not be re-resolved.")
        ),
    )

    result = WhatsAppService.send_template(
        SimpleNamespace(id=123),
        to="5493624200001",
        template_name="merchant_marketing",
        template_language="es_AR",
        body_parameters=["Cliente", "Producto", "Mensaje"],
        branch_id=456,
        connection=connection,
    )

    assert result["messages"][0]["id"] == "wamid.tenant-owned-phone"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == (
        f"https://graph.facebook.com/{WhatsAppService.API_VERSION}/"
        "TENANT_PHONE_NUMBER_ID/messages"
    )
    assert call["headers"]["Authorization"] == "Bearer tenant-specific-meta-token"
    assert call["payload"]["to"] == "5493624200001"
    assert call["payload"]["template"]["name"] == "merchant_marketing"
    assert call["payload"]["template"]["language"]["code"] == "es_AR"
