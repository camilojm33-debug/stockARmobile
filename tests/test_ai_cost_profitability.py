import json
from datetime import datetime, timedelta

import pytest

from app import Company, db
from services.ai_agent.cost_service import estimate_ai_cost
from services.ai_agent.profitability_service import (
    configured_usd_to_ars,
    plan_catalog_profitability,
    plan_monthly_price_ars,
    profitability_snapshot,
)
from services.ai_agent.usage_service import cost_snapshot, record_ai_usage
from stockarmobile.models.conversations import Conversation, ConversationMessage


def _company(name="Empresa IA Test"):
    company = Company(
        name=name,
        active=True,
        preferences_json=json.dumps({
            "ai_agent": {
                "plan_code": "pro",
                "status": "ACTIVA",
            }
        }),
    )
    db.session.add(company)
    db.session.flush()
    return company


def _assistant_message(company, conversation, *, created_at, metadata=None, content="Respuesta IA"):
    message = ConversationMessage(
        conversation_id=conversation.id,
        company_id=company.id,
        sender_type="assistant",
        role="assistant",
        content=content,
        metadata_json=metadata or {},
        created_at=created_at,
    )
    db.session.add(message)
    db.session.flush()
    return message


# Final integration coverage for AI cost and profitability telemetry.\ndef test_estimate_ai_cost_uses_provider_model_pricing(monkeypatch):
    monkeypatch.setenv(
        "AI_TOKEN_PRICING_JSON",
        json.dumps({
            "openai:gpt-5-mini": {
                "input_per_1m_usd": 1.5,
                "output_per_1m_usd": 6.0,
            }
        }),
    )

    result = estimate_ai_cost({
        "provider": "OpenAI",
        "model": "gpt-5-mini",
        "input_tokens": 2_000_000,
        "output_tokens": 500_000,
    })

    assert result["priced"] is True
    assert result["input_rate_per_1m_usd"] == 1.5
    assert result["output_rate_per_1m_usd"] == 6.0
    assert result["estimated_cost_usd"] == pytest.approx(6.0)
    assert result["total_tokens"] == 2_500_000


@pytest.mark.parametrize(
    ("pricing", "provider", "model"),
    [
        (
            {"gpt-5-mini": {"input_per_1m_usd": 2, "output_per_1m_usd": 4}},
            "openai",
            "gpt-5-mini",
        ),
        (
            {"openai": {"gpt-5-mini": {"input_per_1m_usd": 3, "output_per_1m_usd": 5}}},
            "openai",
            "gpt-5-mini",
        ),
        (
            {"openai": {"default": {"input_per_1m_usd": 7, "output_per_1m_usd": 9}}},
            "openai",
            "other-model",
        ),
        (
            {"default": {"input_per_1m_usd": 11, "output_per_1m_usd": 13}},
            "unknown",
            "unknown-model",
        ),
    ],
)
def test_estimate_ai_cost_supports_pricing_fallbacks(monkeypatch, pricing, provider, model):
    monkeypatch.setenv("AI_TOKEN_PRICING_JSON", json.dumps(pricing))

    result = estimate_ai_cost({
        "provider": provider,
        "model": model,
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    })

    assert result["priced"] is True
    assert result["estimated_cost_usd"] == pytest.approx(
        result["input_rate_per_1m_usd"] + result["output_rate_per_1m_usd"]
    )


def test_estimate_ai_cost_marks_missing_pricing_as_unpriced(monkeypatch):
    monkeypatch.delenv("AI_TOKEN_PRICING_JSON", raising=False)

    result = estimate_ai_cost({
        "provider": "openai",
        "model": "gpt-5-mini",
        "input_tokens": 1000,
        "output_tokens": 500,
    })

    assert result["priced"] is False
    assert result["estimated_cost_usd"] == 0.0


def test_record_ai_usage_persists_cost_telemetry_and_is_idempotent(app, monkeypatch):
    monkeypatch.setenv(
        "AI_TOKEN_PRICING_JSON",
        json.dumps({
            "openai:gpt-5-mini": {
                "input_per_1m_usd": 2,
                "output_per_1m_usd": 8,
            }
        }),
    )
    with app.app_context():
        company = _company()
        conversation = Conversation(
            company_id=company.id,
            channel="web",
            status="open",
            metadata_json={},
        )
        db.session.add(conversation)
        db.session.flush()
        message = _assistant_message(
            company,
            conversation,
            created_at=datetime(2026, 9, 20, 12, 0, 0),
        )

        telemetry = {
            "provider": "openai",
            "model": "gpt-5-mini",
            "provider_calls": 2,
            "tool_rounds": 1,
            "input_tokens": 1_000_000,
            "output_tokens": 500_000,
            "total_tokens": 1_500_000,
        }

        assert record_ai_usage(
            company_id=company.id,
            agent_id=7,
            conversation_id=conversation.id,
            user_id=None,
            interaction_type="analista",
            message_id=message.id,
            telemetry=telemetry,
        ) is True
        assert record_ai_usage(
            company_id=company.id,
            agent_id=7,
            conversation_id=conversation.id,
            user_id=None,
            interaction_type="analista",
            message_id=message.id,
            telemetry=telemetry,
        ) is False

        db.session.commit()
        refreshed = db.session.get(ConversationMessage, message.id)
        stored = refreshed.metadata_json["ai_usage_telemetry"]

        assert refreshed.metadata_json["ai_usage_recorded"] is True
        assert stored["provider_calls"] == 2
        assert stored["tool_rounds"] == 1
        assert stored["input_tokens"] == 1_000_000
        assert stored["output_tokens"] == 500_000
        assert stored["total_tokens"] == 1_500_000
        assert stored["cost"]["priced"] is True
        assert stored["cost"]["estimated_cost_usd"] == pytest.approx(6.0)


def test_cost_snapshot_aggregates_tokens_cost_and_agent_model(app, monkeypatch):
    monkeypatch.setenv(
        "AI_TOKEN_PRICING_JSON",
        json.dumps({
            "openai:gpt-5-mini": {
                "input_per_1m_usd": 2,
                "output_per_1m_usd": 8,
            }
        }),
    )
    with app.app_context():
        company = _company()
        conversation = Conversation(
            company_id=company.id,
            channel="web",
            status="open",
            metadata_json={},
        )
        db.session.add(conversation)
        db.session.flush()

        current = datetime(2026, 9, 20, 12, 0, 0)
        _assistant_message(
            company,
            conversation,
            created_at=current,
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "asistente",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "input_tokens": 1_000_000,
                    "output_tokens": 500_000,
                    "total_tokens": 1_500_000,
                    "cost": {
                        "priced": True,
                        "estimated_cost_usd": 6.0,
                    },
                },
            },
        )
        _assistant_message(
            company,
            conversation,
            created_at=current + timedelta(minutes=1),
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "marketing",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "input_tokens": 500_000,
                    "output_tokens": 250_000,
                    "total_tokens": 750_000,
                    "cost": {
                        "priced": True,
                        "estimated_cost_usd": 3.0,
                    },
                },
            },
        )
        _assistant_message(
            company,
            conversation,
            created_at=current + timedelta(minutes=2),
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "asistente",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "total_tokens": 1500,
                },
            },
        )
        _assistant_message(
            company,
            conversation,
            created_at=current - timedelta(days=31),
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "asistente",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "input_tokens": 999_999,
                    "output_tokens": 999_999,
                    "total_tokens": 1_999_998,
                    "cost": {
                        "priced": True,
                        "estimated_cost_usd": 99.0,
                    },
                },
            },
        )

        snapshot = cost_snapshot(company.id, now=current)

        assert snapshot["period"] == "2026-09"
        assert snapshot["estimated_cost_usd"] == pytest.approx(9.0)
        assert snapshot["input_tokens"] == 1_501_000
        assert snapshot["output_tokens"] == 750_500
        assert snapshot["total_tokens"] == 2_251_500
        assert snapshot["priced_interactions"] == 2
        assert snapshot["unpriced_interactions"] == 1
        assert snapshot["by_agent"]["asistente"] == pytest.approx(6.0)
        assert snapshot["by_agent"]["marketing"] == pytest.approx(3.0)
        assert snapshot["by_model"]["gpt-5-mini"] == pytest.approx(9.0)
        assert snapshot["by_model"]["gpt-4o-mini"] == pytest.approx(0.0)


def test_profitability_snapshot_ready_with_fx_and_agent_costs(app, monkeypatch):
    with app.app_context():
        company = _company()
        conversation = Conversation(
            company_id=company.id,
            channel="web",
            status="open",
            metadata_json={},
        )
        db.session.add(conversation)
        db.session.flush()

        current = datetime(2026, 9, 20, 12, 0, 0)
        _assistant_message(
            company,
            conversation,
            created_at=current,
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "analista",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost": {
                        "priced": True,
                        "estimated_cost_usd": 0.2,
                    },
                },
            },
        )

        snapshot = profitability_snapshot(
            company,
            now=current,
            usd_to_ars=1200,
        )

        assert snapshot["status"] == "ready"
        assert snapshot["plan_code"] == "pro"
        assert snapshot["plan_list_price_ars"] == 110000.0
        assert snapshot["estimated_cost_usd"] == 0.2
        assert snapshot["estimated_cost_ars"] == 240.0
        assert snapshot["estimated_gross_contribution_ars"] == 109760.0
        assert snapshot["estimated_margin_percent"] == pytest.approx(99.7818)
        assert snapshot["by_agent_usd"]["analista"] == 0.2
        assert snapshot["by_agent_ars"]["analista"] == 240.0
        assert snapshot["plan_revenue_basis"] == "listed_plan_price"


def test_profitability_snapshot_reports_missing_fx(app, monkeypatch):
    monkeypatch.delenv("AI_USD_TO_ARS", raising=False)
    with app.app_context():
        company = _company()
        conversation = Conversation(
            company_id=company.id,
            channel="web",
            status="open",
            metadata_json={},
        )
        db.session.add(conversation)
        db.session.flush()
        _assistant_message(
            company,
            conversation,
            created_at=datetime(2026, 9, 20, 12, 0, 0),
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "asistente",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "cost": {"priced": True, "estimated_cost_usd": 1.0},
                },
            },
        )

        snapshot = profitability_snapshot(company, now=datetime(2026, 9, 20), usd_to_ars=None)

        assert snapshot["status"] == "missing_usd_to_ars"
        assert snapshot["estimated_cost_usd"] == 1.0
        assert snapshot["estimated_cost_ars"] is None
        assert snapshot["estimated_gross_contribution_ars"] is None
        assert snapshot["estimated_margin_percent"] is None
        assert snapshot["fx_configured"] is False


def test_profitability_snapshot_reports_missing_provider_pricing(app):
    with app.app_context():
        company = _company()
        conversation = Conversation(
            company_id=company.id,
            channel="web",
            status="open",
            metadata_json={},
        )
        db.session.add(conversation)
        db.session.flush()
        _assistant_message(
            company,
            conversation,
            created_at=datetime(2026, 9, 20, 12, 0, 0),
            metadata={
                "ai_usage_recorded": True,
                "agent_key": "asistente",
                "ai_usage_telemetry": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "cost": {"priced": False, "estimated_cost_usd": 0.0},
                },
            },
        )

        snapshot = profitability_snapshot(company, now=datetime(2026, 9, 20), usd_to_ars=1200)

        assert snapshot["status"] == "missing_provider_pricing"
        assert snapshot["cost_pricing_configured"] is False
        assert snapshot["estimated_cost_ars"] is None
        assert snapshot["estimated_gross_contribution_ars"] is None
        assert snapshot["estimated_margin_percent"] is None


def test_profitability_snapshot_reports_no_ai_plan(app):
    with app.app_context():
        company = Company(
            name="Empresa sin plan IA",
            active=True,
            preferences_json=json.dumps({"ai_agent": {}}),
        )
        db.session.add(company)
        db.session.commit()

        snapshot = profitability_snapshot(
            company,
            now=datetime(2026, 9, 20),
            usd_to_ars=1200,
        )

        assert snapshot["status"] == "no_ai_plan"
        assert snapshot["plan_code"] is None
        assert snapshot["plan_list_price_ars"] == 0.0
        assert snapshot["estimated_cost_usd"] == 0.0
        assert snapshot["usage"]["period"] == "2026-09"


@pytest.mark.parametrize(
    ("plan_code", "expected"),
    [
        ("inicio", 11385.0),
        ("vendedor", 27885.0),
        ("negocio", 45885.0),
        ("pro", 110000.0),
    ],
)
def test_plan_monthly_price_ars_matches_catalog(plan_code, expected):
    from services.ai_agent.usage_service import AI_PLAN_BY_CODE

    assert plan_monthly_price_ars(AI_PLAN_BY_CODE[plan_code]) == expected


def test_configured_usd_to_ars_reads_explicit_value_and_env(monkeypatch):
    monkeypatch.setenv("AI_USD_TO_ARS", "1425.50")

    assert configured_usd_to_ars() == 1425.50
    assert configured_usd_to_ars(1200) == 1200.0


def test_plan_catalog_profitability_exposes_all_plans():
    catalog = plan_catalog_profitability(usd_to_ars=1200)

    assert [item["plan_code"] for item in catalog] == [
        "inicio",
        "vendedor",
        "negocio",
        "pro",
    ]
    assert all(item["revenue_basis"] == "listed_plan_price" for item in catalog)
    assert all(item["usd_to_ars"] == 1200.0 for item in catalog)


def test_cost_snapshot_ignores_unrecorded_assistant_responses(app):
    with app.app_context():
        company = _company()
        conversation = Conversation(
            company_id=company.id,
            channel="web",
            status="open",
            metadata_json={},
        )
        db.session.add(conversation)
        db.session.flush()

        _assistant_message(
            company,
            conversation,
            created_at=datetime(2026, 9, 20, 12, 0, 0),
            metadata={},
        )

        snapshot = cost_snapshot(
            company.id,
            now=datetime(2026, 9, 20),
        )

        assert snapshot["estimated_cost_usd"] == 0.0
        assert snapshot["input_tokens"] == 0
        assert snapshot["output_tokens"] == 0
        assert snapshot["priced_interactions"] == 0
        assert snapshot["unpriced_interactions"] == 0


def test_profitability_snapshot_reports_no_ai_usage(app):
    with app.app_context():
        company = _company()

        snapshot = profitability_snapshot(
            company,
            now=datetime(2026, 9, 20),
            usd_to_ars=1200,
        )

        assert snapshot["status"] == "no_ai_usage"
        assert snapshot["estimated_cost_usd"] == 0.0
        assert snapshot["estimated_cost_ars"] == 0.0
        assert snapshot["estimated_gross_contribution_ars"] == 110000.0
        assert snapshot["estimated_margin_percent"] == 100.0
        assert snapshot["cost_pricing_configured"] is False
