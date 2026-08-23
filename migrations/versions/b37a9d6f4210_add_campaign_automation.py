"""add campaign automation

Revision ID: b37a9d6f4210
Revises: af6d9e784b21
Create Date: 2026-08-22 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "b37a9d6f4210"
down_revision = "af6d9e784b21"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "automation_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "offer_stage",
                sa.String(length=20),
                nullable=False,
                server_default="ready",
            )
        )
        batch_op.add_column(sa.Column("targeting_profile", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "target_total",
                sa.Integer(),
                nullable=False,
                server_default="50",
            )
        )
        batch_op.add_column(
            sa.Column(
                "batch_size",
                sa.Integer(),
                nullable=False,
                server_default="10",
            )
        )
        batch_op.add_column(
            sa.Column(
                "follow_up_days",
                sa.Integer(),
                nullable=False,
                server_default="7",
            )
        )
        batch_op.add_column(
            sa.Column(
                "contact_cooldown_days",
                sa.Integer(),
                nullable=False,
                server_default="90",
            )
        )
        batch_op.add_column(
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "last_automation_run_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("last_automation_error", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("last_run_summary", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("campaign_recipients") as batch_op:
        batch_op.add_column(sa.Column("fit_score", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("fit_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("selection_source", sa.String(length=30), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("campaign_recipients") as batch_op:
        batch_op.drop_column("selection_source")
        batch_op.drop_column("fit_reason")
        batch_op.drop_column("fit_score")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("completed_at")
        batch_op.drop_column("last_run_summary")
        batch_op.drop_column("last_automation_error")
        batch_op.drop_column("last_automation_run_at")
        batch_op.drop_column("activated_at")
        batch_op.drop_column("contact_cooldown_days")
        batch_op.drop_column("follow_up_days")
        batch_op.drop_column("batch_size")
        batch_op.drop_column("target_total")
        batch_op.drop_column("targeting_profile")
        batch_op.drop_column("offer_stage")
        batch_op.drop_column("automation_enabled")
