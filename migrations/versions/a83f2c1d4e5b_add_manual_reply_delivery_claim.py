"""add idempotent manual reply delivery claim

Revision ID: a83f2c1d4e5b
Revises: c7e9f1a2b3d4
Create Date: 2026-09-07 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "a83f2c1d4e5b"
down_revision = "c7e9f1a2b3d4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("email_reply") as batch_op:
        batch_op.add_column(sa.Column("reply_delivery_status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("reply_message_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("reply_sending_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("reply_last_error", sa.Text(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_email_reply_reply_message_id", ["reply_message_id"]
        )


def downgrade():
    with op.batch_alter_table("email_reply") as batch_op:
        batch_op.drop_constraint("uq_email_reply_reply_message_id", type_="unique")
        batch_op.drop_column("reply_last_error")
        batch_op.drop_column("reply_sending_started_at")
        batch_op.drop_column("reply_message_id")
        batch_op.drop_column("reply_delivery_status")
