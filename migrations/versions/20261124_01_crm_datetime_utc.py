"""Normalize tenant CRM datetime-local fields to UTC-naive storage.

Before this revision, HTML datetime-local values were parsed as naive datetimes
and persisted directly. They represented the company's wall clock, but the CRM
compared them with UTC-naive timestamps from utcnow(). Convert only those two
user-entered fields using each company's configured timezone. Creation,
completion and audit timestamps were already stored as UTC and are untouched.
"""
from datetime import timezone
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


revision = "20261124_01_crm_utc"
down_revision = "20261123_02_lead_import_rows"
branch_labels = None
depends_on = None

_DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"
_UTC = timezone.utc


def _resolve_timezone(name):
    try:
        return ZoneInfo(str(name or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(_DEFAULT_TIMEZONE)


def _convert_column(bind, table_name, column_name, *, to_utc):
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name) or not inspector.has_table("companies"):
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in columns or "company_id" not in columns:
        return

    last_id = 0
    select_sql = sa.text(
        f"SELECT record.id, record.{column_name} AS stored_value, "
        "COALESCE(NULLIF(company.timezone, ''), :default_timezone) AS timezone_name "
        f"FROM {table_name} AS record "
        "JOIN companies AS company ON company.id = record.company_id "
        f"WHERE record.id > :last_id AND record.{column_name} IS NOT NULL "
        "ORDER BY record.id LIMIT 500"
    )
    update_sql = sa.text(
        f"UPDATE {table_name} SET {column_name} = :converted_value WHERE id = :record_id"
    )

    while True:
        rows = bind.execute(
            select_sql,
            {"default_timezone": _DEFAULT_TIMEZONE, "last_id": last_id},
        ).mappings().all()
        if not rows:
            break

        updates = []
        for row in rows:
            stored_value = row["stored_value"]
            tz = _resolve_timezone(row["timezone_name"])
            if to_utc:
                aware_value = (
                    stored_value.astimezone(_UTC)
                    if stored_value.tzinfo is not None
                    else stored_value.replace(tzinfo=tz).astimezone(_UTC)
                )
                converted = aware_value.replace(tzinfo=None)
            else:
                aware_value = (
                    stored_value.replace(tzinfo=_UTC)
                    if stored_value.tzinfo is None
                    else stored_value.astimezone(_UTC)
                )
                converted = aware_value.astimezone(tz).replace(tzinfo=None)
            updates.append({"record_id": row["id"], "converted_value": converted})
            last_id = row["id"]

        bind.execute(update_sql, updates)


def upgrade():
    bind = op.get_bind()
    _convert_column(bind, "crm_opportunities", "expected_close_date", to_utc=True)
    _convert_column(bind, "crm_activities", "due_at", to_utc=True)


def downgrade():
    bind = op.get_bind()
    _convert_column(bind, "crm_opportunities", "expected_close_date", to_utc=False)
    _convert_column(bind, "crm_activities", "due_at", to_utc=False)
