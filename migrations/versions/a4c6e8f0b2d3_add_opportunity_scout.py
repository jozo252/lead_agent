"""add opportunity scout

Revision ID: a4c6e8f0b2d3
Revises: f2a4c6d8e0b1
Create Date: 2026-09-04 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "a4c6e8f0b2d3"
down_revision = "f2a4c6d8e0b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "business_line",
                sa.String(length=30),
                nullable=False,
                server_default="general",
            )
        )
        batch_op.add_column(
            sa.Column(
                "scout_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("scout_queries", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("last_scout_run_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_scout_error", sa.Text(), nullable=True))

    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("business_line", sa.String(length=30), nullable=False),
        sa.Column("source_name", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.String(length=1500), nullable=False),
        sa.Column("search_query", sa.String(length=400), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("published_text", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("fit_score", sa.Integer(), nullable=False),
        sa.Column("fit_reason", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "source_url",
            name="uq_opportunity_campaign_source_url",
        ),
    )
    op.create_index("ix_opportunities_campaign_id", "opportunities", ["campaign_id"])
    op.create_index("ix_opportunities_business_line", "opportunities", ["business_line"])
    op.create_index("ix_opportunities_status", "opportunities", ["status"])

    op.create_table(
        "scout_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("query_count", sa.Integer(), nullable=False),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("refreshed_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "run_date", name="uq_scout_run_campaign_date"),
    )
    op.create_index("ix_scout_runs_campaign_id", "scout_runs", ["campaign_id"])
    op.create_index("ix_scout_runs_status", "scout_runs", ["status"])


def downgrade():
    op.drop_index("ix_scout_runs_status", table_name="scout_runs")
    op.drop_index("ix_scout_runs_campaign_id", table_name="scout_runs")
    op.drop_table("scout_runs")
    op.drop_index("ix_opportunities_status", table_name="opportunities")
    op.drop_index("ix_opportunities_business_line", table_name="opportunities")
    op.drop_index("ix_opportunities_campaign_id", table_name="opportunities")
    op.drop_table("opportunities")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("last_scout_error")
        batch_op.drop_column("last_scout_run_at")
        batch_op.drop_column("scout_queries")
        batch_op.drop_column("scout_enabled")
        batch_op.drop_column("business_line")
