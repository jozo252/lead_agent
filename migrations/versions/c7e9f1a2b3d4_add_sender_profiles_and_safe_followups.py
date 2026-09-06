"""Add sender identities, dated website checks and one-shot follow-ups.

Revision ID: c7e9f1a2b3d4
Revises: b6d7e8f9a0b1
"""
from alembic import op
import sqlalchemy as sa

revision = "c7e9f1a2b3d4"
down_revision = "b6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sender_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("config_key", sa.String(40), nullable=False, unique=True),
        sa.Column("sender_name", sa.String(100)),
        sa.Column("sender_email", sa.String(255)),
        sa.Column("signature", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_synced_at", sa.DateTime()),
        sa.Column("last_sync_error", sa.Text()),
    )
    # A separate table avoids rebuilding the large RPO companies table.
    op.create_table(
        "company_website_checks",
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("evidence", sa.JSON()),
        sa.Column("searched_queries", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.Column("website_url", sa.Text()),
    )
    op.create_index("ix_company_website_checks_status", "company_website_checks", ["status"])
    op.create_index("ix_company_website_checks_checked_at", "company_website_checks", ["checked_at"])
    for table in ("campaigns", "outbound_emails", "email_reply"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("sender_profile_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(f"fk_{table}_sender_profile_id", "sender_profiles", ["sender_profile_id"], ["id"])
            batch.create_index(f"ix_{table}_sender_profile_id", ["sender_profile_id"])
            if table == "campaigns":
                batch.add_column(sa.Column("require_no_website", sa.Boolean(), nullable=False, server_default=sa.false()))
                batch.add_column(sa.Column("follow_up_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
                batch.add_column(sa.Column("follow_up_subject_template", sa.String(255)))
                batch.add_column(sa.Column("follow_up_body_template", sa.Text()))
                batch.add_column(sa.Column("follow_up_approved_at", sa.DateTime()))
    op.create_table(
        "campaign_followups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_recipient_id", sa.Integer(), sa.ForeignKey("campaign_recipients.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("original_outbound_id", sa.Integer(), sa.ForeignKey("outbound_emails.id"), nullable=False),
        sa.Column("sender_profile_id", sa.Integer(), sa.ForeignKey("sender_profiles.id"), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column("message_id", sa.String(255), unique=True),
        sa.Column("sending_started_at", sa.DateTime()),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_campaign_followups_due_at", "campaign_followups", ["due_at"])
    op.create_index("ix_campaign_followups_status", "campaign_followups", ["status"])


def downgrade():
    # Downgrade removes the NEW feature's history. Back up before rolling back.
    op.drop_table("campaign_followups")
    for table in ("email_reply", "outbound_emails", "campaigns"):
        with op.batch_alter_table(table) as batch:
            if table == "campaigns":
                for column in ("follow_up_approved_at", "follow_up_body_template", "follow_up_subject_template", "follow_up_enabled", "require_no_website"):
                    batch.drop_column(column)
            batch.drop_index(f"ix_{table}_sender_profile_id")
            batch.drop_constraint(f"fk_{table}_sender_profile_id", type_="foreignkey")
            batch.drop_column("sender_profile_id")
    op.drop_table("company_website_checks")
    op.drop_table("sender_profiles")
