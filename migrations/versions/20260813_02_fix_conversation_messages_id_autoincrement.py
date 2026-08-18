"""Fix conversation_messages primary key autogeneration for SQLite/PostgreSQL

Revision ID: 20260813_02_fix_conversation_messages_id_autoincrement
Revises: 20260813_01_ai_agent_conversations_tables
Create Date: 2026-08-13

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260813_02_fix_conversation_messages_id_autoincrement"
down_revision = "20260813_01_ai_agent_conversations_tables"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversation_messages", schema=None) as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
            autoincrement=True,
        )


def downgrade():
    with op.batch_alter_table("conversation_messages", schema=None) as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
            autoincrement=True,
        )
