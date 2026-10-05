"""Backfill one consent row for every existing SaaS lead."""
from datetime import datetime, timezone
import secrets

from alembic import op
import sqlalchemy as sa

revision = "20261005_01_saas_consent_backfill"
down_revision = "20261003_01_saas_consent_tokens"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    meta = sa.MetaData()
    leads = sa.Table(
        "saas_leads",
        meta,
        sa.Column("id", sa.Integer),
        sa.Column("email_consent_status", sa.String(20)),
        sa.Column("whatsapp_consent_status", sa.String(20)),
        sa.Column("phone_consent_status", sa.String(20)),
    )
    consents = sa.Table(
        "saas_lead_consents",
        meta,
        sa.Column("lead_id", sa.Integer),
    )

    rows = bind.execute(
        sa.select(
            leads.c.id,
            leads.c.email_consent_status,
            leads.c.whatsapp_consent_status,
            leads.c.phone_consent_status,
        ).where(
            ~sa.exists(
                sa.select(1).where(consents.c.lead_id == leads.c.id)
            )
        )
    ).mappings().all()

    if not rows:
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    payload = []
    for row in rows:
        email = str(row["email_consent_status"] or "unknown")
        whatsapp = str(row["whatsapp_consent_status"] or "unknown")
        phone = str(row["phone_consent_status"] or "unknown")
        payload.append(
            {
                "lead_id": row["id"],
                "email_status": email,
                "whatsapp_status": whatsapp,
                "phone_status": phone,
                "email_source": "backfill_existing_status" if email == "opted_in" else None,
                "whatsapp_source": "backfill_existing_status" if whatsapp == "opted_in" else None,
                "unsubscribe_token": secrets.token_urlsafe(48),
                "consent_token": None,
                "created_at": now,
                "updated_at": now,
            }
        )

    bind.execute(
        sa.text(
            "INSERT INTO saas_lead_consents "
            "(lead_id,email_status,whatsapp_status,phone_status,email_source,whatsapp_source,"
            "unsubscribe_token,consent_token,created_at,updated_at) "
            "VALUES (:lead_id,:email_status,:whatsapp_status,:phone_status,:email_source,:whatsapp_source,"
            ":unsubscribe_token,:consent_token,:created_at,:updated_at)"
        ),
        payload,
    )


def downgrade():
    pass
