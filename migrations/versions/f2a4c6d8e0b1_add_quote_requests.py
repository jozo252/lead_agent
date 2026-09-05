"""add quote requests

Revision ID: f2a4c6d8e0b1
Revises: f91b6a2c7d10
Create Date: 2026-09-04 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "f2a4c6d8e0b1"
down_revision = "f91b6a2c7d10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quote_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("email_reply_id", sa.Integer(), nullable=True),
        sa.Column("outbound_email_id", sa.Integer(), nullable=True),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("vat_text", sa.String(length=100), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("sender_signature", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("approval_token", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("sending_started_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["email_reply_id"], ["email_reply.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["outbound_email_id"], ["outbound_emails.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_token"),
        sa.UniqueConstraint("email_reply_id"),
        sa.UniqueConstraint("outbound_email_id"),
    )
    op.create_index("ix_quote_requests_lead_id", "quote_requests", ["lead_id"])
    op.create_index("ix_quote_requests_status", "quote_requests", ["status"])


def downgrade():
    op.drop_index("ix_quote_requests_status", table_name="quote_requests")
    op.drop_index("ix_quote_requests_lead_id", table_name="quote_requests")
    op.drop_table("quote_requests")
