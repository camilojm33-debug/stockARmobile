"""Normalize the Vendor Webchat to fixed shipping.

Revision ID: 20260922_02_vendor_fixed_shipping
Revises: 20260922_01_remove_legacy_shipping_percent
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "20260922_02_vendor_fixed_shipping"
down_revision = "20260922_01_remove_legacy_shipping_percent"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    companies = sa.table(
        "companies",
        sa.column("id", sa.Integer),
        sa.column("preferences_json", sa.Text),
    )
    rows = bind.execute(sa.select(companies.c.id, companies.c.preferences_json)).fetchall()

    for company_id, raw_preferences in rows:
        if not raw_preferences:
            continue
        try:
            payload = json.loads(raw_preferences)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        ai = payload.get("ai_agent")
        if not isinstance(ai, dict):
            continue
        vendor = ai.get("vendor_options")
        if not isinstance(vendor, dict):
            continue
        if vendor.get("shipping_mode") != "fixed":
            vendor["shipping_mode"] = "fixed"
            bind.execute(
                companies.update()
                .where(companies.c.id == company_id)
                .values(preferences_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            )

    op.alter_column(
        "quote_deliveries",
        "shipping_source",
        server_default="fixed",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        "quote_deliveries",
        "shipping_source",
        server_default="manual",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
