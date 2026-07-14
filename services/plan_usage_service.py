"""Calculo de uso y limites de planes para tenant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from services.plan_service import PlanService
from services.subscription_service import SubscriptionService


@dataclass
class UsageMetric:
    key: str
    label: str
    used: int
    limit: int
    percent: int
    remaining: int
    bar_class: str


class PlanUsageService:
    RESOURCE_PRODUCTS = "products"
    RESOURCE_CLIENTS = "clients"
    RESOURCE_USERS = "users"

    @staticmethod
    def _bar_class(percent: int) -> str:
        if percent < 70:
            return "bg-success"
        if percent < 90:
            return "bg-warning"
        return "bg-danger"

    @staticmethod
    def _safe_percent(used: int, limit: int) -> int:
        if not limit or limit <= 0:
            return 0
        return max(0, min(100, int(round((used / limit) * 100))))

    @staticmethod
    def _active_plan_for_company(company_id: int):
        from app import db

        PlanService.ensure_defaults(db.session)
        subscription = SubscriptionService.active_subscription_for_company(company_id)
        plan = getattr(subscription, "plan", None)
        if plan is None:
            plan = PlanService.get_plan(code="trial")
        return subscription, plan

    @staticmethod
    def _resource_counts(company_id: int):
        from app import Client, Product, User

        users_count = User.query.filter(
            User.company_id == company_id,
            User.active.is_(True),
            User.role != "superadmin",
        ).count()
        products_count = Product.query.filter(
            Product.company_id == company_id,
            Product.active.is_(True),
        ).count()
        clients_count = Client.query.filter(
            Client.company_id == company_id,
            Client.active.is_(True),
        ).count()
        return {
            PlanUsageService.RESOURCE_USERS: int(users_count or 0),
            PlanUsageService.RESOURCE_PRODUCTS: int(products_count or 0),
            PlanUsageService.RESOURCE_CLIENTS: int(clients_count or 0),
        }

    @staticmethod
    def _resource_limit(plan, resource_key: str) -> int:
        if plan is None:
            return 0
        if resource_key == PlanUsageService.RESOURCE_USERS:
            return int(plan.max_users or 0)
        if resource_key == PlanUsageService.RESOURCE_PRODUCTS:
            return int(plan.max_products or 0)
        if resource_key == PlanUsageService.RESOURCE_CLIENTS:
            return int(plan.max_clients or 0)
        return 0

    @staticmethod
    def _growth_rate_and_days_left(company_id: int, resource_key: str, used: int, limit: int):
        from app import Client, Product, User, db

        if limit <= 0:
            return None

        if resource_key == PlanUsageService.RESOURCE_USERS:
            model = User
            query = User.query.filter(User.company_id == company_id, User.role != "superadmin")
            created_col = User.created_at
        elif resource_key == PlanUsageService.RESOURCE_PRODUCTS:
            model = Product
            query = Product.query.filter(Product.company_id == company_id)
            created_col = Product.created_at
        elif resource_key == PlanUsageService.RESOURCE_CLIENTS:
            model = Client
            query = Client.query.filter(Client.company_id == company_id)
            created_col = Client.created_at
        else:
            return None

        stats = query.with_entities(
            db.func.count(model.id),
            db.func.min(created_col),
            db.func.max(created_col),
        ).first()
        total = int((stats[0] or 0) if stats else 0)
        min_created = stats[1] if stats else None
        max_created = stats[2] if stats else None
        if total < 2 or not min_created or not max_created:
            return None

        span_days = max((max_created - min_created).total_seconds() / 86400.0, 0.0)
        if span_days <= 0:
            return None

        growth_rate = (total - 1) / span_days
        if growth_rate <= 0:
            return None

        remaining = max(limit - used, 0)
        return remaining / growth_rate

    @classmethod
    def usage_snapshot(cls, company_id: int):
        subscription, plan = cls._active_plan_for_company(company_id)
        counts = cls._resource_counts(company_id)

        metrics = []
        for key, label in [
            (cls.RESOURCE_USERS, "Usuarios"),
            (cls.RESOURCE_PRODUCTS, "Productos"),
            (cls.RESOURCE_CLIENTS, "Clientes"),
        ]:
            used = counts[key]
            limit = cls._resource_limit(plan, key)
            percent = cls._safe_percent(used, limit)
            remaining = max(limit - used, 0)
            metrics.append(
                UsageMetric(
                    key=key,
                    label=label,
                    used=used,
                    limit=limit,
                    percent=percent,
                    remaining=remaining,
                    bar_class=cls._bar_class(percent),
                )
            )

        max_percent = max((metric.percent for metric in metrics), default=0)
        warning_90 = max_percent >= 90

        recommendation = None
        if max_percent >= 80 and plan is not None:
            plans = PlanService.all_commercial_plans()
            current_index = next((idx for idx, p in enumerate(plans) if p.id == plan.id), -1)
            if current_index >= 0 and current_index + 1 < len(plans):
                recommendation = plans[current_index + 1]

        days_candidates = []
        for metric in metrics:
            days_left = cls._growth_rate_and_days_left(company_id, metric.key, metric.used, metric.limit)
            if days_left is not None:
                days_candidates.append(days_left)
        estimated_days = int(round(min(days_candidates))) if days_candidates else None

        return {
            "subscription": subscription,
            "plan": plan,
            "metrics": metrics,
            "warning_90": warning_90,
            "max_percent": max_percent,
            "recommendation_plan": recommendation,
            "estimated_days": estimated_days,
        }

    @classmethod
    def can_create(cls, company_id: int, resource_key: str):
        snapshot = cls.usage_snapshot(company_id)
        metric = next((item for item in snapshot["metrics"] if item.key == resource_key), None)
        if metric is None:
            return True, None
        if metric.limit > 0 and metric.used >= metric.limit:
            resource_name = metric.label.lower()
            return False, f"Has alcanzado el limite de {resource_name} permitido por tu plan. Actualiza tu suscripcion para continuar."
        return True, None
