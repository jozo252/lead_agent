"""add hubspot sync ids

Revision ID: f91b6a2c7d10
Revises: d1c0a4e5f6b7
Create Date: 2026-08-29 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "f91b6a2c7d10"
down_revision = "d1c0a4e5f6b7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("lead") as batch_op:
        batch_op.add_column(
            sa.Column("hubspot_contact_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("hubspot_company_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("hubspot_synced_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "ix_lead_hubspot_contact_id", ["hubspot_contact_id"], unique=False
        )
        batch_op.create_index(
            "ix_lead_hubspot_company_id", ["hubspot_company_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("lead") as batch_op:
        batch_op.drop_index("ix_lead_hubspot_company_id")
        batch_op.drop_index("ix_lead_hubspot_contact_id")
        batch_op.drop_column("hubspot_synced_at")
        batch_op.drop_column("hubspot_company_id")
        batch_op.drop_column("hubspot_contact_id")
