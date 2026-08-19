"""legacy lead schema baseline

Revision ID: ca786b56f464
Revises:
Create Date: 2026-08-17 06:35:34.462941

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ca786b56f464'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lead",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("website", sa.String(length=300), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=100), nullable=True),
        sa.Column("google_place_id", sa.String(length=200), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("business_status", sa.String(length=100), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("company_segment", sa.String(length=100), nullable=True),
        sa.Column("work_type", sa.String(length=100), nullable=True),
        sa.Column("work_subtype", sa.String(length=150), nullable=True),
        sa.Column("lead_score", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("reason_to_contact", sa.Text(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("suggested_message", sa.Text(), nullable=True),
        sa.Column("last_contacted_at", sa.DateTime(), nullable=True),
        sa.Column("next_follow_up_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "google_place_id",
            name="uq_lead_google_place_id",
        ),
    )
    op.create_table(
        "email_reply",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=True),
        sa.Column("from_email", sa.String(length=255), nullable=True),
        sa.Column("from_name", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("postmark_message_id", sa.String(length=255), nullable=True),
        sa.Column("mailbox_hash", sa.String(length=255), nullable=True),
        sa.Column("ai_reply_draft", sa.Text(), nullable=True),
        sa.Column("reply_sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["lead_id"], ["lead.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "lead_activity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("activity_type", sa.String(length=50), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["lead_id"], ["lead.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("lead_activity")
    op.drop_table("email_reply")
    op.drop_table("lead")
