"""Persist the fulfillment branch selected for each public vendor conversation."""
from alembic import op
import sqlalchemy as sa


revision = "20261105_01_vendor_branch"
down_revision = "20261102_02_company_memberships"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("conversations")}
    if "branch_id" not in columns:
        op.add_column("conversations", sa.Column("branch_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_conversations_branch_id_branches",
            "conversations",
            "branches",
            ["branch_id"],
            ["id"],
            ondelete="SET NULL",
        )
    indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("conversations")}
    if "ix_conversations_company_branch" not in indexes:
        op.create_index("ix_conversations_company_branch", "conversations", ["company_id", "branch_id"])

    # Backfill old vendor chats to an active company branch, preferring default.
    # The correlated query works on both PostgreSQL and SQLite.
    bind.execute(sa.text(
        """
        UPDATE conversations
           SET branch_id = (
               SELECT b.id
                 FROM branches AS b
                WHERE b.company_id = conversations.company_id
                  AND b.active IS TRUE
                ORDER BY b.is_default DESC, b.id ASC
                LIMIT 1
           )
         WHERE branch_id IS NULL
           AND channel IN ('whatsapp', 'webchat')
           AND EXISTS (
               SELECT 1 FROM branches AS b
                WHERE b.company_id = conversations.company_id
                  AND b.active IS TRUE
           )
        """
    ))


def downgrade():
    bind = op.get_bind()
    indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("conversations")}
    if "ix_conversations_company_branch" in indexes:
        op.drop_index("ix_conversations_company_branch", table_name="conversations")
    foreign_keys = {item.get("name") for item in sa.inspect(bind).get_foreign_keys("conversations")}
    if "fk_conversations_branch_id_branches" in foreign_keys:
        op.drop_constraint("fk_conversations_branch_id_branches", "conversations", type_="foreignkey")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("conversations")}
    if "branch_id" in columns:
        op.drop_column("conversations", "branch_id")
