"""Cost estimation for AI provider telemetry.

Rates are intentionally configuration-driven. No provider price is hardcoded here,
so changing a provider/model price never requires a code deployment.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_CURRENCY = "USD"
PRICING_ENV = "AI_TOKEN_PRICING_JSON"

def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")

def _pricing() -> dict[str, Any]:
    raw = os.getenv(PRICING_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

def _find_rate(pricing: dict[str, Any], provider: str, model: str) -> dict[str, Any] | None:
    provider_key = provider.strip().lower()
    model_key = model.strip().lower()
    candidates = [pricing.get(f"{provider_key}:{model_key}"), pricing.get(model_key)]
    provider_config = pricing.get(provider_key)
    if isinstance(provider_config, dict):
        candidates.extend([provider_config.get(model_key), provider_config.get("default")])
    candidates.append(pricing.get("default"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return None

def estimate_ai_cost(telemetry: dict[str, Any] | None) -> dict[str, Any]:
    """Estimate provider cost from recorded tokens and configured per-1M rates."""
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    provider = str(telemetry.get("provider") or "").strip().lower()
    model = str(telemetry.get("model") or "").strip()
    input_tokens = int(telemetry.get("input_tokens") or 0)
    output_tokens = int(telemetry.get("output_tokens") or 0)
    total_tokens = int(telemetry.get("total_tokens") or input_tokens + output_tokens)
    rate = _find_rate(_pricing(), provider, model) if provider and model else None
    input_rate = _decimal(rate.get("input_per_1m_usd")) if rate else Decimal("0")
    output_rate = _decimal(rate.get("output_per_1m_usd")) if rate else Decimal("0")
    cost = (Decimal(input_tokens) / Decimal(1_000_000) * input_rate + Decimal(output_tokens) / Decimal(1_000_000) * output_rate)
    return {
        "currency": DEFAULT_CURRENCY,
        "priced": bool(rate is not None and (input_rate or output_rate)),
        "input_rate_per_1m_usd": float(input_rate),
        "output_rate_per_1m_usd": float(output_rate),
        "estimated_cost_usd": float(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
