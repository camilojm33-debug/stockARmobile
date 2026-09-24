"""Profitability projection for AI plans.

This module is observational: it does not change billing, access, plan limits, or
subscription state. Plan revenue is represented as the configured/listed monthly
price, not as confirmed cash collected. AI provider cost remains configurable.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from services.ai_agent.usage_service import AI_PLAN_BY_CODE, cost_snapshot, current_plan

FX_ENV = "AI_USD_TO_ARS"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def plan_monthly_price_ars(plan: dict[str, Any] | None) -> Decimal:
    """Parse the configured display price such as '$172.390 / mes' into ARS."""
    if not plan:
        return Decimal("0")
    raw = str(plan.get("price") or "")
    number = raw.split("/", 1)[0].replace("$", "").replace(" ", "").strip()
    # Current plan prices use '.' as the thousands separator.
    if "." in number and "," not in number:
        number = number.replace(".", "")
    elif "," in number and "." not in number:
        number = number.replace(",", "")
    else:
        number = number.replace(".", "").replace(",", "")
    return _decimal(number)


def configured_usd_to_ars(value: Any = None) -> Decimal:
    """Return the configured USD->ARS conversion factor, never a live FX quote."""
    if value is not None:
        return _decimal(value)
    return _decimal(os.getenv(FX_ENV, "").strip())


def profitability_snapshot(
    company,
    *,
    now=None,
    usd_to_ars: Any = None,
) -> dict[str, Any]:
    """Build a current-month profitability projection for one company."""
    plan = current_plan(company)
    if plan is None:
        return {
            "period": None,
            "plan_code": None,
            "plan_name": None,
            "plan_list_price_ars": 0.0,
            "plan_revenue_basis": "listed_plan_price",
            "estimated_cost_usd": 0.0,
            "usd_to_ars": 0.0,
            "estimated_cost_ars": None,
            "estimated_gross_contribution_ars": None,
            "estimated_margin_percent": None,
            "fx_configured": False,
            "cost_pricing_configured": False,
            "status": "no_ai_plan",
            "by_agent_usd": {},
            "by_agent_ars": None,
            "usage": cost_snapshot(company.id, now=now),
        }

    usage = cost_snapshot(company.id, now=now)
    plan_price = plan_monthly_price_ars(plan)
    fx = configured_usd_to_ars(usd_to_ars)
    cost_usd = _decimal(usage.get("estimated_cost_usd"))
    priced_interactions = int(usage.get("priced_interactions") or 0)
    unpriced_interactions = int(usage.get("unpriced_interactions") or 0)
    total_interactions = priced_interactions + unpriced_interactions
    pricing_complete = total_interactions > 0 and unpriced_interactions == 0
    cost_calculable = total_interactions == 0 or pricing_complete
    fx_configured = fx > 0

    # Never turn unknown provider pricing into a fake zero cost or 100% margin.
    # With no AI usage at all, the measured cost for the period is genuinely zero.
    cost_ars = cost_usd * fx if fx_configured and cost_calculable else None
    contribution_ars = plan_price - cost_ars if cost_ars is not None else None
    margin_percent = (
        (contribution_ars / plan_price * Decimal("100"))
        if contribution_ars is not None and plan_price > 0
        else None
    )

    by_agent_usd = {
        key: round(float(value), 8)
        for key, value in (usage.get("by_agent") or {}).items()
    }
    by_agent_ars = (
        {key: round(float(_decimal(value) * fx), 8) for key, value in by_agent_usd.items()}
        if fx_configured and cost_calculable
        else None
    )

    if total_interactions == 0:
        status = "no_ai_usage"
    elif unpriced_interactions > 0:
        status = "missing_provider_pricing"
    elif not fx_configured:
        status = "missing_usd_to_ars"
    else:
        status = "ready"

    return {
        "period": usage.get("period"),
        "plan_code": plan["code"],
        "plan_name": plan["name"],
        "plan_list_price_ars": float(plan_price),
        "plan_revenue_basis": "listed_plan_price",
        "estimated_cost_usd": round(float(cost_usd), 8),
        "usd_to_ars": float(fx) if fx_configured else None,
        "estimated_cost_ars": round(float(cost_ars), 8) if cost_ars is not None else None,
        "estimated_gross_contribution_ars": (
            round(float(contribution_ars), 8) if contribution_ars is not None else None
        ),
        "estimated_margin_percent": (
            round(float(margin_percent), 4) if margin_percent is not None else None
        ),
        "fx_configured": fx_configured,
        "cost_pricing_configured": pricing_complete,
        "status": status,
        "by_agent_usd": by_agent_usd,
        "by_agent_ars": by_agent_ars,
        "usage": usage,
    }


def plan_catalog_profitability(*, usd_to_ars: Any = None) -> list[dict[str, Any]]:
    """Return static economics metadata for every AI plan.

    This intentionally contains no company usage. It is useful to render plan
    economics without pretending that listed price equals collected revenue.
    """
    fx = configured_usd_to_ars(usd_to_ars)
    return [
        {
            "plan_code": plan["code"],
            "plan_name": plan["name"],
            "plan_list_price_ars": float(plan_monthly_price_ars(plan)),
            "agents": list(plan.get("agents") or ()),
            "usd_to_ars": float(fx) if fx > 0 else None,
            "revenue_basis": "listed_plan_price",
        }
        for plan in AI_PLAN_BY_CODE.values()
    ]
