"""add RPO and outreach schema

Revision ID: 76990c29294e
Revises: ca786b56f464
Create Date: 2026-08-17 06:35:35.915904

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '76990c29294e'
down_revision = 'ca786b56f464'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ico", sa.String(length=20), nullable=True),
        sa.Column("official_name", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=100), nullable=True),
        sa.Column("legal_form", sa.String(length=255), nullable=True),
        sa.Column("sk_nace_code", sa.String(length=20), nullable=True),
        sa.Column("sk_nace_name", sa.String(length=500), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("employee_count_source", sa.String(length=100), nullable=True),
        sa.Column("company_type", sa.String(length=255), nullable=True),
        sa.Column("services", sa.JSON(), nullable=True),
        sa.Column("markets", sa.JSON(), nullable=True),
        sa.Column("works_abroad", sa.Boolean(), nullable=True),
        sa.Column("regions", sa.JSON(), nullable=True),
        sa.Column("subcontractor_need", sa.String(length=20), nullable=True),
        sa.Column("outreach_relevant", sa.Boolean(), nullable=True),
        sa.Column("analysis_reason", sa.Text(), nullable=True),
        sa.Column("analysis_evidence", sa.JSON(), nullable=True),
        sa.Column("website_analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contacts_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("municipality", sa.String(length=255), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("street", sa.String(length=500), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("established_on", sa.Date(), nullable=True),
        sa.Column("terminated_on", sa.Date(), nullable=True),
        sa.Column("rpo_actualized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rpo_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("companies") as batch_op:
        batch_op.create_index("ix_companies_company_type", ["company_type"])
        batch_op.create_index("ix_companies_ico", ["ico"], unique=True)
        batch_op.create_index("ix_companies_municipality", ["municipality"])
        batch_op.create_index("ix_companies_official_name", ["official_name"])
        batch_op.create_index(
            "ix_companies_outreach_relevant",
            ["outreach_relevant"],
        )
        batch_op.create_index("ix_companies_sk_nace_code", ["sk_nace_code"])

    op.create_table(
        "sync_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True)),
        sa.Column("sync_started_at", sa.DateTime(timezone=True)),
        sa.Column("next_url", sa.Text()),
        sa.Column("processed_records", sa.Integer(), nullable=False),
        sa.Column("fetched_records", sa.Integer(), nullable=False),
        sa.Column("skipped_records", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_sync_states_name"),
    )
    op.create_table(
        "company_activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_activities_company_id",
        "company_activities",
        ["company_id"],
    )
    op.create_table(
        "company_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("contact_type", sa.String(length=30), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("label", sa.String(length=100)),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("confidence_score", sa.Float()),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_contacts_company_id",
        "company_contacts",
        ["company_id"],
    )
    op.create_index(
        "ix_company_contacts_contact_type",
        "company_contacts",
        ["contact_type"],
    )
    op.create_table(
        "company_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("resource_url", sa.Text()),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "source_id",
            name="uq_company_source",
        ),
    )
    op.create_index(
        "ix_company_sources_company_id",
        "company_sources",
        ["company_id"],
    )
    op.create_index(
        "ix_company_sources_external_id",
        "company_sources",
        ["external_id"],
    )
    op.create_index(
        "ix_company_sources_source_type",
        "company_sources",
        ["source_type"],
    )

    with op.batch_alter_table("lead") as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_lead_company_id_companies",
            "companies",
            ["company_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_lead_company_id",
            ["company_id"],
        )

    with op.batch_alter_table("email_reply") as batch_op:
        batch_op.add_column(sa.Column("imap_message_id", sa.String(length=255)))
        batch_op.create_unique_constraint(
            "uq_email_reply_imap_message_id",
            ["imap_message_id"],
        )

    op.create_table(
        "outbound_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["lead.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_outbound_emails_message_id"),
    )
    op.create_index(
        "ix_outbound_emails_lead_id",
        "outbound_emails",
        ["lead_id"],
    )


def downgrade():
    op.drop_index("ix_outbound_emails_lead_id", table_name="outbound_emails")
    op.drop_table("outbound_emails")

    with op.batch_alter_table("email_reply") as batch_op:
        batch_op.drop_constraint(
            "uq_email_reply_imap_message_id",
            type_="unique",
        )
        batch_op.drop_column("imap_message_id")

    with op.batch_alter_table("lead") as batch_op:
        batch_op.drop_constraint("uq_lead_company_id", type_="unique")
        batch_op.drop_constraint(
            "fk_lead_company_id_companies",
            type_="foreignkey",
        )
        batch_op.drop_column("company_id")

    op.drop_index("ix_company_sources_source_type", table_name="company_sources")
    op.drop_index("ix_company_sources_external_id", table_name="company_sources")
    op.drop_index("ix_company_sources_company_id", table_name="company_sources")
    op.drop_table("company_sources")
    op.drop_index("ix_company_contacts_contact_type", table_name="company_contacts")
    op.drop_index("ix_company_contacts_company_id", table_name="company_contacts")
    op.drop_table("company_contacts")
    op.drop_index(
        "ix_company_activities_company_id",
        table_name="company_activities",
    )
    op.drop_table("company_activities")
    op.drop_table("sync_states")
    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_index("ix_companies_sk_nace_code")
        batch_op.drop_index("ix_companies_outreach_relevant")
        batch_op.drop_index("ix_companies_official_name")
        batch_op.drop_index("ix_companies_municipality")
        batch_op.drop_index("ix_companies_ico")
        batch_op.drop_index("ix_companies_company_type")
    op.drop_table("companies")
