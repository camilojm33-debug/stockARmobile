import copy
import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///qa_e2e.db"
os.environ["WHATSAPP_APP_SECRET"] = "test-secret"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "qa-verify-token"

from app import Company, app, db
from services.ai_agent.config_service import choose_agent
from stockarmobile.models.conversations import Conversation, ConversationMessage


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
    original_preferences = copy.deepcopy(company.preferences_json)

    def apply_whatsapp_cfg(phone_id="PHONE_ID_QA", enabled=True):
        prefs = copy.deepcopy(company.preferences_json)
        if isinstance(prefs, str):
            try:
                prefs = json.loads(prefs) if prefs.strip() else {}
            except json.JSONDecodeError:
                prefs = {}
        if not isinstance(prefs, dict):
            prefs = {}
        ai = copy.deepcopy(prefs.get("ai_agent", {})) if isinstance(prefs.get("ai_agent", {}), dict) else {}
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
        company.preferences_json = json.dumps(prefs, separators=(",", ":"))
        db.session.add(company)
        db.session.commit()

    def restore():
        original_value = copy.deepcopy(original_preferences)
        if original_value is None:
            company.preferences_json = "{}"
        elif isinstance(original_value, str):
            company.preferences_json = original_value
        else:
            company.preferences_json = json.dumps(original_value, separators=(",", ":"))
        db.session.add(company)
        db.session.commit()

    try:
        apply_whatsapp_cfg()
        client = app.test_client()

        valid_raw = json.dumps(payload()).encode()
        valid_resp = client.post("/api/whatsapp/webhook", data=valid_raw, headers={"X-Hub-Signature-256": sign(valid_raw)})
        print("VALID_SIGNATURE_STATUS", valid_resp.status_code)
        print("VALID_SIGNATURE_JSON", valid_resp.get_json())

        bad_raw = json.dumps(payload()).encode()
        bad_resp = client.post("/api/whatsapp/webhook", data=bad_raw, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        print("INVALID_SIGNATURE_STATUS", bad_resp.status_code)
        print("INVALID_SIGNATURE_JSON", bad_resp.get_json())

        no_sig_resp = client.post("/api/whatsapp/webhook", data=bad_raw, headers={})
        print("MISSING_SIGNATURE_STATUS", no_sig_resp.status_code)
        print("MISSING_SIGNATURE_JSON", no_sig_resp.get_json())

        unknown_raw = json.dumps(payload(phone_number_id="PHONE_ID_UNKNOWN")).encode()
        unknown_resp = client.post("/api/whatsapp/webhook", data=unknown_raw, headers={"X-Hub-Signature-256": sign(unknown_raw)})
        print("UNKNOWN_PHONE_STATUS", unknown_resp.status_code)
        print("UNKNOWN_PHONE_JSON", unknown_resp.get_json())

        duplicate_message_id = "wamid.QA.DUP.001"
        duplicate_sender = "5490000000000"
        duplicate_conversation = Conversation.query.filter_by(company_id=company.id, channel="whatsapp", external_conversation_id=duplicate_sender).first()
        if duplicate_conversation is None:
            duplicate_conversation = Conversation(
                company_id=company.id,
                agent_id=choose_agent(company.id, channel="whatsapp").id,
                channel="whatsapp",
                external_conversation_id=duplicate_sender,
                metadata_json={"whatsapp_user": duplicate_sender},
            )
            db.session.add(duplicate_conversation)
            db.session.flush()
        duplicate_row = ConversationMessage(
            conversation_id=duplicate_conversation.id,
            company_id=company.id,
            sender_type="user",
            sender_id=None,
            role="user",
            content="preexisting duplicate",
            content_type="text",
            external_message_id=duplicate_message_id,
            idempotency_key=f"whatsapp:{duplicate_message_id}",
            metadata_json={"preseeded": True},
        )
        db.session.add(duplicate_row)
        db.session.commit()

        calls = {"process": 0, "send": 0}

        def fake_process(**kwargs):
            calls["process"] += 1
            return {"status": "completed", "content": "ok", "conversation_id": kwargs["conversation_id"], "company_id": kwargs["company_id"]}

        def fake_send(company_obj, *, to, body):
            calls["send"] += 1
            return {"status": "accepted"}

        with patch("whatsapp_agent.AgentRuntime.process", side_effect=fake_process) as runtime_mock, patch("whatsapp_agent.WhatsAppService.send_text", side_effect=fake_send):
            dup_raw = json.dumps(payload(msg_id=duplicate_message_id, body="Reenviado")).encode()
            dup_resp = client.post("/api/whatsapp/webhook", data=dup_raw, headers={"X-Hub-Signature-256": sign(dup_raw)})
            print("DUPLICATE_PRESEED_STATUS", dup_resp.status_code, dup_resp.get_json())
            print("DUPLICATE_PRESEED_RUNTIME_CALLS", runtime_mock.call_count)
            print("DUPLICATE_PRESEED_SEND_CALLS", calls["send"])
            runtime_mock.assert_not_called()

        raw2 = json.dumps(payload(msg_id="wamid.QA.002", body="Otro mensaje")).encode()
        with patch("whatsapp_agent.AgentRuntime.process", side_effect=fake_process) as runtime_mock, patch("whatsapp_agent.WhatsAppService.send_text", side_effect=fake_send):
            second_msg = client.post("/api/whatsapp/webhook", data=raw2, headers={"X-Hub-Signature-256": sign(raw2)})
            print("SECOND_MESSAGE_STATUS", second_msg.status_code, second_msg.get_json())
            print("SECOND_MESSAGE_RUNTIME_CALLS", runtime_mock.call_count)

        replay = json.dumps(payload(msg_id="wamid.QA.REPLAY.001", body="Replay")).encode()
        with patch("whatsapp_agent.AgentRuntime.process", side_effect=fake_process) as runtime_mock, patch("whatsapp_agent.WhatsAppService.send_text", side_effect=fake_send):
            replay_first = client.post("/api/whatsapp/webhook", data=replay, headers={"X-Hub-Signature-256": sign(replay)})
            replay_second = client.post("/api/whatsapp/webhook", data=replay, headers={"X-Hub-Signature-256": sign(replay)})
            print("REPLAY_FIRST_STATUS", replay_first.status_code, replay_first.get_json())
            print("REPLAY_SECOND_STATUS", replay_second.status_code, replay_second.get_json())
            print("REPLAY_RUNTIME_CALLS", runtime_mock.call_count)

        malformed_raw = b'{"object":'
        malformed_signed = sign(malformed_raw)
        malformed = client.post("/api/whatsapp/webhook", data=malformed_raw, headers={"X-Hub-Signature-256": malformed_signed})
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

        concurrency_message_id = "wamid.QA.CONCUR.001"
        thread_payload = json.dumps(payload(msg_id=concurrency_message_id, body="Concurrente")).encode()
        process_lock = {"count": 0}

        def fake_concurrent_process(**kwargs):
            process_lock["count"] += 1
            return {"status": "completed", "content": "ok", "conversation_id": kwargs["conversation_id"], "company_id": kwargs["company_id"]}

        with patch("whatsapp_agent.AgentRuntime.process", side_effect=fake_concurrent_process), patch("whatsapp_agent.WhatsAppService.send_text", side_effect=fake_send):
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(client.post, "/api/whatsapp/webhook", data=thread_payload, headers={"X-Hub-Signature-256": sign(thread_payload)}),
                        executor.submit(client.post, "/api/whatsapp/webhook", data=thread_payload, headers={"X-Hub-Signature-256": sign(thread_payload)}),
                    ]
                    result_list = [future.result() for future in futures]
                count_rows = db.session.query(ConversationMessage).filter(
                    ConversationMessage.company_id == company.id,
                    ConversationMessage.external_message_id == concurrency_message_id,
                ).count()
                print("CONCURRENCY_RESULTS", [r.status_code for r in result_list])
                print("CONCURRENCY_RUNTIME_CALLS", process_lock["count"])
                print("CONCURRENCY_DB_ROWS", count_rows)
                print("CONCURRENCY_NOTE", "requires concurrency-capable SQLite/driver to prove serialization; harness records the outcome without claiming a false positive.")
            except Exception as exc:  # pragma: no cover - local QA harness only
                print("CONCURRENCY_EXCEPTION", type(exc).__name__, str(exc)[:200])
                print("CONCURRENCY_NOTE", "requires concurrency-capable SQLite/driver to prove serialization; harness records the outcome without claiming a false positive.")

        ids = [m.external_message_id for m in db.session.query(ConversationMessage).filter(ConversationMessage.company_id == company.id).all() if m.external_message_id]
        print("DB_EXTERNAL_MESSAGE_IDS", ids)
    finally:
        restore()
