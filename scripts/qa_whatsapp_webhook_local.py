import hashlib
import hmac
import json
import os
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///qa_e2e.db"
os.environ["WHATSAPP_APP_SECRET"] = "test-secret"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "qa-verify-token"

from app import Company, app, db
from stockarmobile.models.conversations import ConversationMessage


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(os.environ["WHATSAPP_APP_SECRET"].encode(), raw, hashlib.sha256).hexdigest()


def payload(phone_number_id="PHONE_ID_QA", sender="5490000000000", msg_id="wamid.QA.001", body="Hola"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": "QA User"}, "wa_id": sender}],
                            "messages": [{
                                "from": sender,
                                "id": msg_id,
                                "timestamp": "1710000000",
                                "type": "text",
                                "text": {"body": body},
                            }],
                        }
                    }
                ],
            }
        ],
    }


with app.app_context():
    company = Company.query.get(1)
    original = json.loads(company.preferences_json) if isinstance(company.preferences_json, str) and company.preferences_json.strip() else {}
    original_ai = dict(original.get("ai_agent", {})) if isinstance(original, dict) else {}

    def apply_whatsapp_cfg(phone_id="PHONE_ID_QA", enabled=True):
        prefs = json.loads(company.preferences_json) if isinstance(company.preferences_json, str) and company.preferences_json.strip() else {}
        ai = dict(prefs.get("ai_agent", {})) if isinstance(prefs, dict) else {}
        ai["enabled"] = True
        ai["whatsapp"] = {
            "enabled": enabled,
            "phone_number_id": phone_id,
            "business_account_id": "BA_QA_001",
            "display_phone_number": "+54 9 0000 000000",
            "template_name": "hello_world",
            "template_language": "es_AR",
            "access_token_encrypted": "",
        }
        prefs["ai_agent"] = ai
        company.preferences_json = json.dumps(prefs)
        db.session.add(company)
        db.session.commit()

    def restore():
        company.preferences_json = json.dumps({"ai_agent": original_ai}) if original_ai else "{}"
        db.session.add(company)
        db.session.commit()

    try:
        apply_whatsapp_cfg()
        client = app.test_client()

        valid_raw = json.dumps(payload()).encode()
        valid_resp = client.post("/api/whatsapp/webhook", data=valid_raw, headers={"X-Hub-Signature-256": sign(valid_raw)})
        print("VALID_SIGNATURE_STATUS", valid_resp.status_code)
        print("VALID_SIGNATURE_JSON", valid_resp.get_json())

        invalid_raw = json.dumps(payload()).encode()
        invalid_resp = client.post("/api/whatsapp/webhook", data=invalid_raw, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        print("INVALID_SIGNATURE_STATUS", invalid_resp.status_code)
        print("INVALID_SIGNATURE_JSON", invalid_resp.get_json())

        missing_sig_resp = client.post("/api/whatsapp/webhook", data=invalid_raw, headers={})
        print("MISSING_SIGNATURE_STATUS", missing_sig_resp.status_code)
        print("MISSING_SIGNATURE_JSON", missing_sig_resp.get_json())

        unknown_raw = json.dumps(payload(phone_number_id="PHONE_ID_UNKNOWN")).encode()
        unknown_resp = client.post("/api/whatsapp/webhook", data=unknown_raw, headers={"X-Hub-Signature-256": sign(unknown_raw)})
        print("UNKNOWN_PHONE_STATUS", unknown_resp.status_code)
        print("UNKNOWN_PHONE_JSON", unknown_resp.get_json())

        calls = {"process": 0, "send": 0}

        def fake_process(**kwargs):
            calls["process"] += 1
            return {"status": "completed", "content": "ok", "conversation_id": kwargs["conversation_id"], "company_id": kwargs["company_id"]}

        def fake_send(company, *, to, body):
            calls["send"] += 1
            return {"status": "accepted"}

        with patch("whatsapp_agent.AgentRuntime.process", side_effect=fake_process), patch("whatsapp_agent.WhatsAppService.send_text", side_effect=fake_send):
            raw1 = json.dumps(payload(msg_id="wamid.QA.001", body="Hola")).encode()
            first = client.post("/api/whatsapp/webhook", data=raw1, headers={"X-Hub-Signature-256": sign(raw1)})
            second = client.post("/api/whatsapp/webhook", data=raw1, headers={"X-Hub-Signature-256": sign(raw1)})
            print("DUPLICATE_FIRST_STATUS", first.status_code, first.get_json())
            print("DUPLICATE_SECOND_STATUS", second.status_code, second.get_json())
            print("PROCESS_COUNT_AFTER_DUP", calls["process"])
            print("SEND_COUNT_AFTER_DUP", calls["send"])

            raw2 = json.dumps(payload(msg_id="wamid.QA.002", body="Otro mensaje")).encode()
            second_msg = client.post("/api/whatsapp/webhook", data=raw2, headers={"X-Hub-Signature-256": sign(raw2)})
            print("SECOND_MESSAGE_STATUS", second_msg.status_code, second_msg.get_json())
            print("PROCESS_COUNT_AFTER_SECOND_MSG", calls["process"])

        replay = json.dumps(payload(msg_id="wamid.QA.REPLAY.001", body="Replay")).encode()
        with patch("whatsapp_agent.AgentRuntime.process", side_effect=fake_process), patch("whatsapp_agent.WhatsAppService.send_text", side_effect=fake_send):
            replay_first = client.post("/api/whatsapp/webhook", data=replay, headers={"X-Hub-Signature-256": sign(replay)})
            replay_second = client.post("/api/whatsapp/webhook", data=replay, headers={"X-Hub-Signature-256": sign(replay)})
            print("REPLAY_FIRST_STATUS", replay_first.status_code, replay_first.get_json())
            print("REPLAY_SECOND_STATUS", replay_second.status_code, replay_second.get_json())
            print("REPLAY_PROCESS_CALLS", calls["process"])

        malformed = client.post("/api/whatsapp/webhook", data="not-json", headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        print("MALFORMED_STATUS", malformed.status_code)
        print("MALFORMED_JSON", malformed.get_json())

        image_payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "PHONE_ID_QA"},
                        "messages": [{
                            "from": "5490000000000",
                            "id": "wamid.QA.IMAGE",
                            "timestamp": "1710000000",
                            "type": "image",
                            "image": {"caption": "x"},
                        }],
                    }
                }]
            }],
        }
        image_raw = json.dumps(image_payload).encode()
        image_resp = client.post("/api/whatsapp/webhook", data=image_raw, headers={"X-Hub-Signature-256": sign(image_raw)})
        print("UNSUPPORTED_TYPE_STATUS", image_resp.status_code)
        print("UNSUPPORTED_TYPE_JSON", image_resp.get_json())

        ids = [m.external_message_id for m in db.session.query(ConversationMessage).filter(ConversationMessage.company_id == company.id).all() if m.external_message_id]
        print("DB_EXTERNAL_MESSAGE_IDS", ids)
    finally:
        restore()
