"""Operational automation helpers for SuperAdmin SaaS center."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass
class AttentionSignal:
    company_id: int | None
    company_name: str
    reason: str
    severity: str
    detail: str


class SaaSOpsService:
    """Creates automatic CRM records and computes operational signals."""

    @staticmethod
    def _superadmin_actor_id(db_session) -> int | None:
        from app import User

        actor = (
            db_session.query(User)
            .filter(User.role == "superadmin")
            .order_by(User.id.asc())
            .first()
        )
        return actor.id if actor else None

    @classmethod
    def _default_actor_id(cls, db_session, preferred_user_id: int | None = None) -> int | None:
        if preferred_user_id:
            return preferred_user_id
        return cls._superadmin_actor_id(db_session)

    @staticmethod
    def _open_lead_for_identity(db_session, email: str | None, company_name: str | None):
        from app import SaaSLead

        query = db_session.query(SaaSLead).filter(SaaSLead.status.in_(["nuevo", "contactado", "propuesta"]))
        if email:
            return query.filter(SaaSLead.email == email).order_by(SaaSLead.id.desc()).first()
        if company_name:
            return query.filter(SaaSLead.company_name == company_name).order_by(SaaSLead.id.desc()).first()
        return None

    @staticmethod
    def _open_task_exists(db_session, *, company_id: int | None, title: str) -> bool:
        from app import SaaSTask

        query = db_session.query(SaaSTask).filter(
            SaaSTask.title == title,
            SaaSTask.status.in_(["pendiente", "en_progreso", "bloqueada"]),
        )
        if company_id is None:
            query = query.filter(SaaSTask.company_id.is_(None))
        else:
            query = query.filter(SaaSTask.company_id == company_id)
        return query.first() is not None

    @staticmethod
    def _open_alert_exists(db_session, *, company_id: int | None, title: str) -> bool:
        from app import SaaSAlert

        query = db_session.query(SaaSAlert).filter(
            SaaSAlert.title == title,
            SaaSAlert.status == "abierta",
        )
        if company_id is None:
            query = query.filter(SaaSAlert.company_id.is_(None))
        else:
            query = query.filter(SaaSAlert.company_id == company_id)
        return query.first() is not None

    @classmethod
    def create_or_update_lead(
        cls,
        db_session,
        *,
        company_name: str,
        contact_name: str,
        email: str | None,
        phone: str | None,
        source: str,
        notes: str,
        company_id: int | None,
        preferred_user_id: int | None,
    ):
        from app import SaaSLead, utcnow

        actor_id = cls._default_actor_id(db_session, preferred_user_id)
        if actor_id is None:
            return None

        open_lead = cls._open_lead_for_identity(db_session, email=email, company_name=company_name)
        if open_lead is not None:
            open_lead.company_name = (company_name or open_lead.company_name or "Prospecto").strip()[:160]
            open_lead.contact_name = (contact_name or open_lead.contact_name or "Contacto").strip()[:160]
            open_lead.phone = (phone or open_lead.phone or "").strip()[:40] or None
            open_lead.source = (source or open_lead.source or "manual").strip().lower()[:80] or "manual"
            if notes:
                previous = (open_lead.notes or "").strip()
                open_lead.notes = f"{previous}\n{notes}".strip() if previous else notes
            open_lead.company_id = company_id or open_lead.company_id
            open_lead.updated_at = utcnow()
            return open_lead

        lead = SaaSLead(
            company_name=(company_name or "Prospecto").strip()[:160],
            contact_name=(contact_name or "Contacto").strip()[:160],
            email=(email or "").strip().lower()[:160] or None,
            phone=(phone or "").strip()[:40] or None,
            source=(source or "manual").strip().lower()[:80] or "manual",
            status="nuevo",
            priority="media",
            notes=notes.strip() or None,
            company_id=company_id,
            created_by_user_id=actor_id,
            assigned_user_id=actor_id,
        )
        db_session.add(lead)
        db_session.flush()
        return lead

    @classmethod
    def create_task(
        cls,
        db_session,
        *,
        company_id: int | None,
        lead_id: int | None,
        title: str,
        description: str,
        priority: str,
        due_days: int,
        preferred_user_id: int | None,
    ):
        from app import SaaSTask, utcnow

        if cls._open_task_exists(db_session, company_id=company_id, title=title):
            return None

        actor_id = cls._default_actor_id(db_session, preferred_user_id)
        if actor_id is None:
            return None

        task = SaaSTask(
            lead_id=lead_id,
            company_id=company_id,
            title=title[:180],
            description=description,
            status="pendiente",
            priority=priority if priority in {"baja", "media", "alta"} else "media",
            due_at=utcnow() + timedelta(days=max(due_days, 0)),
            assigned_user_id=actor_id,
            created_by_user_id=actor_id,
        )
        db_session.add(task)
        db_session.flush()
        return task

    @classmethod
    def create_alert(
        cls,
        db_session,
        *,
        company_id: int | None,
        lead_id: int | None,
        task_id: int | None,
        title: str,
        message: str,
        category: str,
        severity: str,
        preferred_user_id: int | None,
    ):
        from app import SaaSAlert

        if cls._open_alert_exists(db_session, company_id=company_id, title=title):
            return None

        actor_id = cls._default_actor_id(db_session, preferred_user_id)
        if actor_id is None:
            return None

        alert = SaaSAlert(
            company_id=company_id,
            lead_id=lead_id,
            task_id=task_id,
            title=title[:180],
            message=message,
            category=(category or "operativa").strip().lower()[:40] or "operativa",
            severity=severity if severity in {"baja", "media", "alta", "critica"} else "media",
            status="abierta",
            assigned_user_id=actor_id,
            created_by_user_id=actor_id,
        )
        db_session.add(alert)
        db_session.flush()
        return alert

    @classmethod
    def register_signup(cls, db_session, *, company, user):
        from app import record_audit

        lead = cls.create_or_update_lead(
            db_session,
            company_name=getattr(company, "name", "Nueva empresa"),
            contact_name=getattr(user, "username", "Nuevo usuario"),
            email=getattr(user, "email", None),
            phone=None,
            source="registro",
            notes="Registro automático desde /auth/register.",
            company_id=getattr(company, "id", None),
            preferred_user_id=getattr(user, "id", None),
        )
        task = cls.create_task(
            db_session,
            company_id=getattr(company, "id", None),
            lead_id=getattr(lead, "id", None),
            title=f"Onboarding inicial - {getattr(company, 'name', 'empresa')}",
            description="Revisar activación, uso inicial y riesgo de abandono durante trial.",
            priority="media",
            due_days=2,
            preferred_user_id=getattr(user, "id", None),
        )
        cls.create_alert(
            db_session,
            company_id=getattr(company, "id", None),
            lead_id=getattr(lead, "id", None),
            task_id=getattr(task, "id", None),
            title=f"Nueva empresa registrada: {getattr(company, 'name', 'empresa')}",
            message="Se creó automáticamente un nuevo registro de empresa y requiere seguimiento comercial inicial.",
            category="comercial",
            severity="media",
            preferred_user_id=getattr(user, "id", None),
        )
        record_audit(
            action="saas_activity_signup_auto",
            entity="company",
            entity_id=getattr(company, "id", None),
            company_id=getattr(company, "id", None),
            detail="Automatización SaaS: lead, tarea y alerta generadas por registro.",
            user_id=getattr(user, "id", None),
        )

    @classmethod
    def register_landing_contact(cls, db_session, *, name: str, email: str, message: str):
        from app import record_audit

        lead = cls.create_or_update_lead(
            db_session,
            company_name=name or "Consulta landing",
            contact_name=name or "Contacto landing",
            email=email,
            phone=None,
            source="landing_form",
            notes=(message or "").strip()[:2000],
            company_id=None,
            preferred_user_id=None,
        )
        task = cls.create_task(
            db_session,
            company_id=None,
            lead_id=getattr(lead, "id", None),
            title=f"Responder lead landing: {name or email or 'sin nombre'}",
            description="Lead generado automáticamente desde formulario público de landing.",
            priority="alta",
            due_days=1,
            preferred_user_id=None,
        )
        cls.create_alert(
            db_session,
            company_id=None,
            lead_id=getattr(lead, "id", None),
            task_id=getattr(task, "id", None),
            title="Nuevo lead desde landing",
            message="Ingreso automático desde formulario de contacto de la landing.",
            category="comercial",
            severity="alta",
            preferred_user_id=None,
        )
        record_audit(
            action="saas_activity_landing_lead_auto",
            entity="saas_lead",
            entity_id=getattr(lead, "id", None),
            detail="Automatización SaaS: lead, tarea y alerta desde formulario landing.",
            user_id=cls._default_actor_id(db_session),
            company_id=None,
        )

    @classmethod
    def register_support_ticket(cls, db_session, *, ticket, user):
        from app import record_audit

        company_name = getattr(getattr(ticket, "company", None), "name", None) or f"Empresa #{getattr(ticket, 'company_id', '-') }"
        lead = cls.create_or_update_lead(
            db_session,
            company_name=company_name,
            contact_name=getattr(user, "username", "Usuario"),
            email=getattr(ticket, "email", None),
            phone=None,
            source="support",
            notes=f"Ticket soporte #{getattr(ticket, 'id', '-')}: {getattr(ticket, 'reason', '')}",
            company_id=getattr(ticket, "company_id", None),
            preferred_user_id=getattr(user, "id", None),
        )
        task = cls.create_task(
            db_session,
            company_id=getattr(ticket, "company_id", None),
            lead_id=getattr(lead, "id", None),
            title=f"Atender ticket soporte #{getattr(ticket, 'id', '-')}",
            description=(getattr(ticket, "description", "") or "")[:1000],
            priority="alta",
            due_days=1,
            preferred_user_id=getattr(user, "id", None),
        )
        cls.create_alert(
            db_session,
            company_id=getattr(ticket, "company_id", None),
            lead_id=getattr(lead, "id", None),
            task_id=getattr(task, "id", None),
            title=f"Soporte abierto: {getattr(ticket, 'reason', 'incidente')}",
            message="Ticket de soporte recibido; requiere acción prioritaria.",
            category="soporte",
            severity="alta",
            preferred_user_id=getattr(user, "id", None),
        )
        record_audit(
            action="saas_activity_support_auto",
            entity="support_ticket",
            entity_id=getattr(ticket, "id", None),
            detail="Automatización SaaS: lead, tarea y alerta desde ticket de soporte.",
            user_id=getattr(user, "id", None),
            company_id=getattr(ticket, "company_id", None),
        )
