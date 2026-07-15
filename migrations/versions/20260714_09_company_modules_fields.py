"""Add company module settings fields and user permissions.

Revision ID: 20260714_09_company_modules_fields
Revises: 20260714_08_company_location_contact_fields
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_09_company_modules_fields"
down_revision = "20260714_08_company_location_contact_fields"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "companies"):
        company_columns = [
            ("website", sa.String(length=255), True, None),
            ("social_facebook", sa.String(length=255), True, None),
            ("social_instagram", sa.String(length=255), True, None),
            ("social_tiktok", sa.String(length=255), True, None),
            ("social_youtube", sa.String(length=255), True, None),
            ("social_linkedin", sa.String(length=255), True, None),
            ("language", sa.String(length=20), False, sa.text("'es'")),
            ("timezone", sa.String(length=80), False, sa.text("'America/Argentina/Buenos_Aires'")),
            ("currency", sa.String(length=10), False, sa.text("'ARS'")),
            ("date_format", sa.String(length=20), False, sa.text("'%Y-%m-%d'")),
            ("numbering_format", sa.String(length=20), False, sa.text("'es_AR'")),
            ("printer_settings_json", sa.Text(), True, None),
            ("preferences_json", sa.Text(), True, None),
            ("schedules_json", sa.Text(), True, None),
        ]
        for name, col_type, nullable, server_default in company_columns:
            if not _has_column(bind, "companies", name):
                op.add_column("companies", sa.Column(name, col_type, nullable=nullable, server_default=server_default))

        op.execute("UPDATE companies SET language = COALESCE(language, 'es')")
        op.execute("UPDATE companies SET timezone = COALESCE(timezone, 'America/Argentina/Buenos_Aires')")
        op.execute("UPDATE companies SET currency = COALESCE(currency, 'ARS')")
        op.execute("UPDATE companies SET date_format = COALESCE(date_format, '%Y-%m-%d')")
        op.execute("UPDATE companies SET numbering_format = COALESCE(numbering_format, 'es_AR')")

    if _has_table(bind, "users") and not _has_column(bind, "users", "permissions_json"):
        op.add_column("users", sa.Column("permissions_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "users") and _has_column(bind, "users", "permissions_json"):
        op.drop_column("users", "permissions_json")

    if _has_table(bind, "companies"):
        company_columns = [
            "schedules_json",
            "preferences_json",
            "printer_settings_json",
            "numbering_format",
            "date_format",
            "currency",
            "timezone",
            "language",
            "social_linkedin",
            "social_youtube",
            "social_tiktok",
            "social_instagram",
            "social_facebook",
            "website",
        ]
        for name in company_columns:
            if _has_column(bind, "companies", name):
                op.drop_column("companies", name)
