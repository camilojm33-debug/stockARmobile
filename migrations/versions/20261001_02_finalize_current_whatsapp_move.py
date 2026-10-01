"""Finalize the current SuperAdmin WhatsApp move after the destination was configured."""
from __future__ import annotations

import json
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20261001_02_finalize_current_whatsapp_move"
down_revision = "20261001_01_fix_commercial_whatsapp_state"
branch_labels = None
depends_on = None

DESTINATION_NAME = "StockArMobile Comercial"
MARKER = "whatsapp_commercial"
TARGET_PHONE_ID = "1213985218475043"


def _load(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(bind, company_id, prefs):
    bind.execute(
        sa.text("UPDATE companies SET preferences_json = :prefs WHERE id = :id"),
        {
            "id": company_id,
            "prefs": json.dumps(prefs, ensure_ascii=False, separators=(",", ":")),
        },
    )


def upgrade():
    bind = op.get_bind()

    destination = bind.execute(
        sa.text(
            "SELECT id, preferences_json FROM companies "
            "WHERE name = :name ORDER BY id ASC LIMIT 1"
        ),
        {"name": DESTINATION_NAME},
    ).first()
    if destination is None:
        raise RuntimeError("No existe la empresa interna StockArMobile Comercial.")

    destination_prefs = _load(destination.preferences_json)
    destination_prefs["internal_channel"] = MARKER
    destination_ai = destination_prefs.get("ai_agent")
    if not isinstance(destination_ai, dict):
        destination_ai = {}
    destination_wa = destination_ai.get("whatsapp")
    if not isinstance(destination_wa, dict):
        destination_wa = {}
    destination_wa["enabled"] = True
    destination_wa["phone_number_id"] = str(destination_wa.get("phone_number_id") or TARGET_PHONE_ID).strip()
    destination_ai["whatsapp"] = destination_wa
    destination_ai["enabled"] = True
    destination_prefs["ai_agent"] = destination_ai
    _save(bind, destination.id, destination_prefs)

    target_phone = destination_wa["phone_number_id"]
    if not target_phone:
        raise RuntimeError("El Phone Number ID comercial está vacío.")

    rows = bind.execute(
        sa.text("SELECT id, name, preferences_json FROM companies WHERE active = true AND id <> :id"),
        {"id": destination.id},
    ).fetchall()

    matches = []
    for row in rows:
        prefs = _load(row.preferences_json)
        ai = prefs.get("ai_agent")
        if not isinstance(ai, dict):
            continue
        wa = ai.get("whatsapp")
        if not isinstance(wa, dict):
            continue
        if str(wa.get("phone_number_id") or "").strip() == target_phone:
            matches.append((row, prefs))

    if len(matches) > 1:
        raise RuntimeError(
            "Hay más de una empresa con el Phone Number ID actual; no se desactiva ninguna por seguridad."
        )

    if matches:
        row, prefs = matches[0]
        ai = prefs.get("ai_agent") or {}
        wa = dict(ai.get("whatsapp") or {})
        wa["enabled"] = False
        wa["phone_number_id"] = ""
        wa["business_account_id"] = ""
        wa["display_phone_number"] = ""
        wa["template_name"] = ""
        wa.pop("access_token_encrypted", None)
        ai["whatsapp"] = wa
        ai["whatsapp_enabled"] = False
        prefs["ai_agent"] = ai
        _save(bind, row.id, prefs)


def downgrade():
    pass
