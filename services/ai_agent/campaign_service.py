"""Tenant-scoped campaign persistence and explicit approval transitions."""

from __future__ import annotations

import json
from datetime import datetime

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive


CAMPAIGN_STATUSES = {"BORRADOR", "PENDIENTE_APROBACION", "APROBADA", "EN_PREPARACION", "ENVIADA", "CANCELADA"}


class CampaignService:
    @staticmethod
    def _campaign(company_id: int, campaign_id: int):
        from app import Campaign

        return Campaign.query.filter_by(id=campaign_id, company_id=company_id).first()

    @classmethod
    def create_draft(cls, *, company_id: int, user_id: int, title: str, objective: str, campaign_type: str, content: str, system_data: dict, audience_segment: str, audience_count: int, product_id: int | None = None):
        from app import Campaign, Product, User

        product = None
        if product_id is not None:
            product = Product.query.filter_by(id=product_id, company_id=company_id, active=True).first()
            if product is None:
                raise ValueError("El producto no pertenece a la empresa o no está disponible.")
        creator = User.query.filter_by(id=user_id, company_id=company_id, active=True).first()
        if creator is None:
            creator = User.query.filter(User.company_id == company_id, User.active.is_(True), User.role.in_(["admin", "user"])).order_by(User.id.asc()).first()
        if creator is None:
            raise ValueError("No hay un usuario activo para crear la campaña.")
        context = system_data if isinstance(system_data, dict) else {}
        channel = str(context.get("channel") or "email").strip().lower()
        if channel not in {"email", "whatsapp", "both"}:
            channel = "email"
        scheduled_at = None
        raw_schedule = str(context.get("scheduled_at") or "").strip()
        if raw_schedule:
            try:
                scheduled_at = datetime.fromisoformat(raw_schedule.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                scheduled_at = None
        campaign = Campaign(
            company_id=company_id,
            title=(title or "Campaña propuesta")[:180],
            objective=(objective or "Promoción")[:120],
            campaign_type=(campaign_type or "general")[:80],
            status="BORRADOR",
            content=content or "",
            system_data_json=json.dumps(context, ensure_ascii=False),
            audience_segment=(audience_segment or "No definido")[:120],
            audience_count=max(0, int(audience_count or 0)),
            channel=channel,
            scheduled_at=scheduled_at,
            product_id=product.id if product else None,
            created_by_user_id=creator.id,
        )
        db.session.add(campaign)
        db.session.flush()
        return campaign

    @classmethod
    def update_draft(cls, *, company_id: int, campaign_id: int, title: str, objective: str, content: str, user_id: int):
        campaign = cls._campaign(company_id, campaign_id)
        if campaign is None:
            raise ValueError("Campaña no encontrada para esta empresa.")
        if campaign.status not in {"BORRADOR", "PENDIENTE_APROBACION"}:
            raise ValueError("Solo se pueden editar campañas en borrador o pendientes de aprobación.")
        campaign.title = (title or campaign.title)[:180]
        campaign.objective = (objective or campaign.objective)[:120]
        campaign.content = content or campaign.content
        campaign.created_by_user_id = user_id
        db.session.flush()
        return campaign

    @classmethod
    def transition(cls, *, company_id: int, campaign_id: int, target_status: str, user_id: int):
        campaign = cls._campaign(company_id, campaign_id)
        if campaign is None:
            raise ValueError("Campaña no encontrada para esta empresa.")
        target_status = str(target_status or "").upper()
        transitions = {"BORRADOR": {"PENDIENTE_APROBACION", "CANCELADA"}, "PENDIENTE_APROBACION": {"APROBADA", "BORRADOR", "CANCELADA"}, "APROBADA": {"EN_PREPARACION", "CANCELADA"}, "EN_PREPARACION": {"CANCELADA"}, "ENVIADA": set(), "CANCELADA": set()}
        if target_status not in transitions.get(campaign.status, set()):
            raise ValueError(f"Transición no permitida: {campaign.status} → {target_status}.")
        campaign.status = target_status
        if target_status == "APROBADA":
            campaign.approved_by_user_id = user_id
            campaign.approved_at = utcnow_naive()
        db.session.flush()
        return campaign