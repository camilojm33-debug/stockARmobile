"""Seed the isolated internal company and agent for SuperAdmin commercial WhatsApp."""
from __future__ import annotations

import os
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20260930_01_commercial_whatsapp"
down_revision = "20260927_01_saas_commercial_crm"
branch_labels = None
depends_on = None

COMPANY_NAME = "StockArMobile Comercial"
MARKER = "whatsapp_commercial"
AGENT_NAME = "Comercial IA"


def _now():
    return datetime.utcnow()


def upgrade():
    bind = op.get_bind()

    company_row = bind.execute(
        sa.text(
            "SELECT id, preferences_json FROM companies "
            "WHERE name = :name ORDER BY id ASC LIMIT 1"
        ),
        {"name": COMPANY_NAME},
    ).first()

    if company_row is None:
        preferences = (
            '{"internal_channel":"whatsapp_commercial",'
            '"ai_agent":{"enabled":true,"plan_code":"pro","status":"ACTIVA","ends_at":null,'
            '"whatsapp":{"enabled":false,"phone_number_id":"","business_account_id":"",'
            '"display_phone_number":"","template_name":"","template_language":"es_AR"}}}'
        )
        bind.execute(
            sa.text(
                "INSERT INTO companies "
                "(name, language, timezone, currency, date_format, numbering_format, "
                "preferences_json, business_pin_failed_attempts, active, created_at) "
                "VALUES (:name, :language, :timezone, :currency, :date_format, "
                ":numbering_format, :preferences_json, 0, true, :created_at)"
            ),
            {
                "name": COMPANY_NAME,
                "language": "es",
                "timezone": "America/Argentina/Buenos_Aires",
                "currency": "ARS",
                "date_format": "%Y-%m-%d",
                "numbering_format": "es_AR",
                "preferences_json": preferences,
                "created_at": _now(),
            },
        )
        company_id = bind.execute(
            sa.text(
                "SELECT id FROM companies "
                "WHERE name = :name "
                "AND preferences_json LIKE :marker "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"name": COMPANY_NAME, "marker": "%whatsapp_commercial%"},
        ).scalar_one()
    else:
        company_id = company_row.id
        raw = company_row.preferences_json or ""
        if MARKER not in raw:
            raise RuntimeError(
                "Existe una empresa llamada 'StockArMobile Comercial' que no está "
                "marcada como canal interno; se detiene la migración para evitar "
                "modificar una empresa existente."
            )

    agent_id = bind.execute(
        sa.text(
            "SELECT id FROM agents "
            "WHERE company_id = :company_id AND name = :name "
            "ORDER BY id ASC LIMIT 1"
        ),
        {"company_id": company_id, "name": AGENT_NAME},
    ).scalar()

    if agent_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO agents "
                "(company_id, name, description, active, created_at, updated_at) "
                "VALUES (:company_id, :name, :description, true, :created_at, :updated_at)"
            ),
            {
                "company_id": company_id,
                "name": AGENT_NAME,
                "description": "Agente IA para captación y atención comercial de StockArMobile.",
                "created_at": _now(),
                "updated_at": _now(),
            },
        )
        agent_id = bind.execute(
            sa.text(
                "SELECT id FROM agents "
                "WHERE company_id = :company_id AND name = :name "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"company_id": company_id, "name": AGENT_NAME},
        ).scalar_one()

    existing_config = bind.execute(
        sa.text(
            "SELECT id FROM agent_configurations "
            "WHERE company_id = :company_id AND agent_id = :agent_id "
            "ORDER BY id ASC LIMIT 1"
        ),
        {"company_id": company_id, "agent_id": agent_id},
    ).scalar()

    if existing_config is None:
        model = (os.getenv("GEMINI_MODEL") or "gemini-3.6-flash").strip()
        bind.execute(
            sa.text(
                "INSERT INTO agent_configurations "
                "(agent_id, company_id, model, system_prompt, language, max_tokens, "
                "temperature, created_at, updated_at) "
                "VALUES (:agent_id, :company_id, :model, :system_prompt, 'es-AR', "
                "900, 0.20, :created_at, :updated_at)"
            ),
            {
                "agent_id": agent_id,
                "company_id": company_id,
                "model": model,
                "system_prompt": (
                    "Atendé prospectos de StockArMobile. No uses ni consultes datos de "
                    "comercios clientes. No generes pedidos ni cobros. Explicá el producto "
                    "y guiá al prospecto hacia la contratación. Antes de informar precios o "
                    "planes, consultá la herramienta de oferta comercial."
                ),
                "created_at": _now(),
                "updated_at": _now(),
            },
        )


def downgrade():
    # La empresa interna puede acumular conversaciones y registros durante producción.
    # Para no borrar historial comercial accidentalmente, el downgrade no elimina datos.
    pass
