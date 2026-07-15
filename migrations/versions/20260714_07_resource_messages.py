"""Create resource messages table.

Revision ID: 20260714_07_resource_messages
Revises: 8a0e95fc2189
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_07_resource_messages"
down_revision = "8a0e95fc2189"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "resource_messages"):
        op.create_table(
            "resource_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("category", sa.String(length=40), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_resource_messages_category", "resource_messages", ["category"], unique=False)
        op.create_index("ix_resource_messages_active", "resource_messages", ["active"], unique=False)
        op.create_index("ix_resource_messages_created_at", "resource_messages", ["created_at"], unique=False)

        resource_messages = sa.table(
            "resource_messages",
            sa.column("category", sa.String()),
            sa.column("title", sa.String()),
            sa.column("content", sa.Text()),
            sa.column("sort_order", sa.Integer()),
            sa.column("active", sa.Boolean()),
        )
        op.bulk_insert(
            resource_messages,
            [
                {"category": "whatsapp", "title": "Primer mensaje de WhatsApp", "content": "Hola, te comparto StockArmobile para ordenar ventas, stock y clientes desde un solo lugar. Puedo mostrártelo en una demo breve.", "sort_order": 10, "active": True},
                {"category": "facebook", "title": "Publicación para Facebook", "content": "StockArmobile ayuda a controlar caja, stock y ventas sin planillas ni procesos confusos. Ideal para negocios en crecimiento.", "sort_order": 20, "active": True},
                {"category": "instagram", "title": "Story para Instagram", "content": "Mostrá tu negocio con una herramienta moderna para vender, controlar stock y mejorar la gestión diaria.", "sort_order": 30, "active": True},
                {"category": "email", "title": "Email de presentación", "content": "Hola, te presento StockArmobile: una plataforma para ventas, stock, clientes y reportes que se adapta a celulares y PC.", "sort_order": 40, "active": True},
                {"category": "primer_contacto", "title": "Apertura de conversación", "content": "Hola, quería contarte sobre una herramienta que puede ahorrarte tiempo y errores al vender y controlar inventario.", "sort_order": 50, "active": True},
                {"category": "seguimiento", "title": "Mensaje de seguimiento", "content": "Te escribo para retomar la conversación y compartirte una demo corta con los beneficios más importantes de StockArmobile.", "sort_order": 60, "active": True},
                {"category": "cierre_venta", "title": "Cierre comercial", "content": "Si querés profesionalizar el control de tu negocio, el siguiente paso es activar la prueba y validar el flujo real en operación.", "sort_order": 70, "active": True},
                {"category": "promociones", "title": "Promoción de lanzamiento", "content": "Tenemos prueba gratuita y planes escalables para que empieces sin fricción y con acompañamiento inicial.", "sort_order": 80, "active": True},
                {"category": "clientes_actuales", "title": "Mensaje para clientes actuales", "content": "Para quienes ya usan la plataforma, este es un buen momento para sumar más usuarios y aprovechar el control operativo completo.", "sort_order": 90, "active": True},
            ],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "resource_messages"):
        op.drop_index("ix_resource_messages_created_at", table_name="resource_messages")
        op.drop_index("ix_resource_messages_active", table_name="resource_messages")
        op.drop_index("ix_resource_messages_category", table_name="resource_messages")
        op.drop_table("resource_messages")
