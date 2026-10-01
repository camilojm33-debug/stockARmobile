"""Repair and finalize the SuperAdmin commercial WhatsApp connection state."""
from __future__ import annotations

import json
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20261001_01_fix_commercial_whatsapp_state"
down_revision = "20260930_01_commercial_whatsapp"
branch_labels = None
depends_on = None

COMPANY_NAME = "StockArMobile Comercial"
MARKER = "whatsapp_commercial"


def _now():
    return datetime.utcnow()


def _load_prefs(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(bind, company_id, prefs):
    bind.execute(
        sa.text(
            "UPDATE companies SET preferences_json = :preferences_json WHERE id = :company_id"
        ),
        {
            "company_id": company_id,
            "preferences_json": json.dumps(prefs, ensure_ascii=False, separators=(",", ":")),
        },
    )


def upgrade():
    bind = op.get_bind()

    destination = bind.execute(
        sa.text(
            "SELECT id, preferences_json FROM companies "
            "WHERE name = :name ORDER BY id ASC LIMIT 1"
        ),
        {"name": COMPANY_NAME},
    ).first()
    if destination is None:
        raise RuntimeError("No existe la empresa interna StockArMobile Comercial.")

    destination_prefs = _load_prefs(destination.preferences_json)
    destination_prefs["internal_channel"] = MARKER
    destination_ai = destination_prefs.get("ai_agent")
    if not isinstance(destination_ai, dict):
        destination_ai = {}
    destination_ai["enabled"] = True
    destination_ai["status"] = destination_ai.get("status") or "ACTIVA"
    destination_prefs["ai_agent"] = destination_ai
    _save(bind, destination.id, destination_prefs)

    commercial_wa = destination_ai.get("whatsapp")
    if not isinstance(commercial_wa, dict):
        commercial_wa = {}
    target_phone_id = str(commercial_wa.get("phone_number_id") or "").strip()
    if not target_phone_id:
        return

    candidates = []
    rows = bind.execute(
        sa.text(
            "SELECT id, name, preferences_json FROM companies "
            "WHERE active = true AND id <> :destination_id"
        ),
        {"destination_id": destination.id},
    ).fetchall()
    for row in rows:
        prefs = _load_prefs(row.preferences_json)
        ai = prefs.get("ai_agent")
        if not isinstance(ai, dict):
            continue
        wa = ai.get("whatsapp")
        if not isinstance(wa, dict):
            continue
        if str(wa.get("phone_number_id") or "").strip() == target_phone_id:
            candidates.append((row, prefs))

    if len(candidates) > 1:
        raise RuntimeError(
            "Más de una empresa tiene configurado el Phone Number ID comercial; "
            "se detiene la reparación para evitar desactivar la cuenta equivocada."
        )

    if len(candidates) == 1:
        row, source_prefs = candidates[0]
        source_ai = source_prefs.get("ai_agent")
        if not isinstance(source_ai, dict):
            return
        source_wa = source_ai.get("whatsapp")
        if not isinstance(source_wa, dict):
            return

        source_wa = dict(source_wa)
        source_wa["enabled"] = False
        source_wa["phone_number_id"] = ""
        source_wa["business_account_id"] = ""
        source_wa["display_phone_number"] = ""
        source_wa["template_name"] = ""
        source_ai["whatsapp"] = source_wa
        source_ai["whatsapp_enabled"] = False
        source_prefs["ai_agent"] = source_ai
        _save(bind, row.id, source_prefs)


def downgrade():
    pass
