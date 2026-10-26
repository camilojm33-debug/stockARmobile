"""Servicio de planes comerciales SaaS."""

from __future__ import annotations

from typing import Optional


class PlanService:
    TRIAL_DAYS = 10

    PLAN_CATALOG = [
        {
            "code": "trial",
            "name": "Trial",
            "price": 0.0,
            "currency": "ARS",
            "duration_days": 10,
            "max_users": 2,
            "max_products": 150,
            "max_clients": 250,
            "features_json": "inventario,ventas,clientes,reportes_basicos",
            "state": "active",
        },
        {
            "code": "entrepreneur",
            "name": "Emprendedor",
            "price": 12999.0,
            "currency": "ARS",
            "duration_days": 30,
            "max_users": 3,
            "max_products": 1200,
            "max_clients": 2000,
            "features_json": "inventario,ventas,clientes,reportes,excel",
            "state": "active",
        },
        {
            "code": "business",
            "name": "Negocio",
            "price": 29999.0,
            "currency": "ARS",
            "duration_days": 30,
            "max_users": 8,
            "max_products": 12000,
            "max_clients": 12000,
            "features_json": "inventario,ventas,clientes,compras,caja,reportes,excel,kardex,pricing_controller,promotions,wholesale_pricing",
            "state": "active",
        },
        {
            "code": "premium",
            "name": "Premium",
            "price": 54999.0,
            "currency": "ARS",
            "duration_days": 30,
            "max_users": 50,
            "max_products": 100000,
            "max_clients": 100000,
            "features_json": "all",
            "state": "active",
        },
    ]

    @classmethod
    def ensure_defaults(cls, db_session) -> None:
        """Create missing catalog entries without overwriting administrator choices.

        The catalog is a bootstrap mechanism, not a source of truth after a plan
        has been created. Prices, limits, features and activation state must stay
        under Superadmin control across requests and deployments.
        """
        from app import Plan

        existing_codes = {plan.code for plan in Plan.query.all() if plan.code}
        changed = False
        for plan_payload in cls.PLAN_CATALOG:
            if plan_payload["code"] in existing_codes:
                continue
            db_session.add(Plan(**plan_payload))
            changed = True
        if changed:
            db_session.commit()

    @staticmethod
    def get_plan(plan_id: Optional[int] = None, code: Optional[str] = None):
        from app import Plan

        if plan_id:
            return Plan.query.filter_by(id=plan_id, active=True).first()
        if code:
            return Plan.query.filter_by(code=code, active=True).first()
        return None

    @staticmethod
    def all_commercial_plans():
        from app import Plan

        return Plan.query.filter(Plan.active.is_(True)).order_by(Plan.price.asc()).all()
