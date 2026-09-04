"""link opportunity to crm lead

Revision ID: b6d7e8f9a0b1
Revises: a4c6e8f0b2d3
Create Date: 2026-09-04 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "b6d7e8f9a0b1"
down_revision = "a4c6e8f0b2d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("lead") as batch_op:
        batch_op.add_column(
            sa.Column("suggested_subject", sa.String(length=255), nullable=True)
        )

    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.add_column(sa.Column("lead_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("verification_note", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("contact_source_url", sa.String(length=1500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_opportunities_lead_id_lead",
            "lead",
            ["lead_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_opportunities_lead_id",
            ["lead_id"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.drop_index("ix_opportunities_lead_id")
        batch_op.drop_constraint(
            "fk_opportunities_lead_id_lead",
            type_="foreignkey",
        )
        batch_op.drop_column("converted_at")
        batch_op.drop_column("contact_source_url")
        batch_op.drop_column("verification_note")
        batch_op.drop_column("verified_at")
        batch_op.drop_column("lead_id")

    with op.batch_alter_table("lead") as batch_op:
        batch_op.drop_column("suggested_subject")
