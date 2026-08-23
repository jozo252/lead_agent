"""add campaign delivery locks

Revision ID: d1c0a4e5f6b7
Revises: e42c61a7d8f9
Create Date: 2026-08-23 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "d1c0a4e5f6b7"
down_revision = "e42c61a7d8f9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(
            sa.Column("delivery_lock_token", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("delivery_locked_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("campaign_recipients") as batch_op:
        batch_op.add_column(
            sa.Column("sending_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "ix_campaign_recipient_campaign_attempt",
            ["campaign_id", "sending_started_at"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("campaign_recipients") as batch_op:
        batch_op.drop_index("ix_campaign_recipient_campaign_attempt")
        batch_op.drop_column("sending_started_at")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("delivery_locked_at")
        batch_op.drop_column("delivery_lock_token")
