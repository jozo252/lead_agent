"""add campaigns suppressions and postal locations

Revision ID: af6d9e784b21
Revises: c4a8c99323bd
Create Date: 2026-08-18 15:10:00

"""
from alembic import op
import sqlalchemy as sa


revision = "af6d9e784b21"
down_revision = "c4a8c99323bd"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("offer_type", sa.String(length=30), nullable=False),
        sa.Column("offer_description", sa.Text(), nullable=False),
        sa.Column("subject_template", sa.String(length=255), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("target_filters", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    op.create_table(
        "suppressions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "value",
            name="uq_suppression_scope_value",
        ),
    )
    op.create_index("ix_suppressions_scope", "suppressions", ["scope"])

    op.create_table(
        "postal_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("postal_code", sa.String(length=10), nullable=False),
        sa.Column("place_name", sa.String(length=255), nullable=False),
        sa.Column("search_name", sa.String(length=255), nullable=False),
        sa.Column("admin_name_1", sa.String(length=255), nullable=True),
        sa.Column("admin_name_2", sa.String(length=255), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "postal_code",
            "place_name",
            name="uq_postal_location_place",
        ),
    )
    op.create_index(
        "ix_postal_locations_postal_code",
        "postal_locations",
        ["postal_code"],
    )
    op.create_index(
        "ix_postal_locations_search_name",
        "postal_locations",
        ["search_name"],
    )

    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["company_contacts.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "company_id",
            name="uq_campaign_recipient_company",
        ),
    )
    op.create_index(
        "ix_campaign_recipients_campaign_id",
        "campaign_recipients",
        ["campaign_id"],
    )
    op.create_index(
        "ix_campaign_recipients_company_id",
        "campaign_recipients",
        ["company_id"],
    )
    op.create_index(
        "ix_campaign_recipients_status",
        "campaign_recipients",
        ["status"],
    )

    with op.batch_alter_table("outbound_emails") as batch_op:
        batch_op.add_column(
            sa.Column("campaign_recipient_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_outbound_emails_campaign_recipient",
            "campaign_recipients",
            ["campaign_recipient_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_outbound_emails_campaign_recipient_id",
            ["campaign_recipient_id"],
        )

    with op.batch_alter_table("email_reply") as batch_op:
        batch_op.add_column(
            sa.Column("campaign_recipient_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_email_reply_campaign_recipient",
            "campaign_recipients",
            ["campaign_recipient_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_email_reply_campaign_recipient_id",
            ["campaign_recipient_id"],
        )


def downgrade():
    with op.batch_alter_table("email_reply") as batch_op:
        batch_op.drop_index("ix_email_reply_campaign_recipient_id")
        batch_op.drop_constraint(
            "fk_email_reply_campaign_recipient",
            type_="foreignkey",
        )
        batch_op.drop_column("campaign_recipient_id")

    with op.batch_alter_table("outbound_emails") as batch_op:
        batch_op.drop_index("ix_outbound_emails_campaign_recipient_id")
        batch_op.drop_constraint(
            "fk_outbound_emails_campaign_recipient",
            type_="foreignkey",
        )
        batch_op.drop_column("campaign_recipient_id")

    op.drop_index(
        "ix_campaign_recipients_status",
        table_name="campaign_recipients",
    )
    op.drop_index(
        "ix_campaign_recipients_company_id",
        table_name="campaign_recipients",
    )
    op.drop_index(
        "ix_campaign_recipients_campaign_id",
        table_name="campaign_recipients",
    )
    op.drop_table("campaign_recipients")
    op.drop_index(
        "ix_postal_locations_search_name",
        table_name="postal_locations",
    )
    op.drop_index(
        "ix_postal_locations_postal_code",
        table_name="postal_locations",
    )
    op.drop_table("postal_locations")
    op.drop_index("ix_suppressions_scope", table_name="suppressions")
    op.drop_table("suppressions")
    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_table("campaigns")
