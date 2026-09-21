from datetime import datetime

from app import Company, db
from services.ai_agent.usage_service import usage_snapshot
from stockarmobile.models.conversations import Conversation, ConversationMessage


def test_usage_snapshot_aggregates_recorded_messages_in_database(qa_ai_database):
    company = qa_ai_database["companies"]["vendedor"]
    conversation = Conversation(company_id=company.id, channel="test", status="open")
    db.session.add(conversation)
    db.session.flush()

    db.session.add_all(
        [
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=company.id,
                sender_type="agent",
                role="assistant",
                content="respuesta 1",
                content_type="text",
                created_at=datetime.now(),
                metadata_json={"ai_usage_recorded": True, "agent_key": "asistente"},
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=company.id,
                sender_type="agent",
                role="assistant",
                content="respuesta 2",
                content_type="text",
                created_at=datetime.now(),
                metadata_json={"ai_usage_recorded": True, "agent_key": "vendedor"},
            ),
            ConversationMessage(
                conversation_id=conversation.id,
                company_id=company.id,
                sender_type="agent",
                role="assistant",
                content="histórica",
                content_type="text",
                created_at=datetime.now(),
                metadata_json={"agent_key": "asistente"},
            ),
        ]
    )
    db.session.commit()

    snapshot = usage_snapshot(company.id, now=datetime.now())

    assert snapshot["used_usage"] == 2
    assert snapshot["by_agent"] == {"asistente": 1, "vendedor": 1}
    assert snapshot["remaining_usage"] == 298
