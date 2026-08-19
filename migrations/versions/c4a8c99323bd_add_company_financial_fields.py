"""add company financial fields

Revision ID: c4a8c99323bd
Revises: 76990c29294e
Create Date: 2026-08-17 14:15:00

"""
from alembic import op
import sqlalchemy as sa


revision = "c4a8c99323bd"
down_revision = "76990c29294e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies") as batch_op:
        batch_op.add_column(sa.Column("financial_year", sa.Integer()))
        batch_op.add_column(sa.Column("annual_revenue", sa.Numeric(20, 2)))
        batch_op.add_column(sa.Column("annual_total_income", sa.Numeric(20, 2)))
        batch_op.add_column(sa.Column("annual_profit", sa.Numeric(20, 2)))
        batch_op.add_column(
            sa.Column("financial_statement_submitted_on", sa.Date())
        )
        batch_op.add_column(sa.Column("financials_status", sa.String(40)))
        batch_op.add_column(
            sa.Column("financials_checked_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("ruz_accounting_entity_id", sa.Integer()))
        batch_op.add_column(sa.Column("financial_statement_id", sa.Integer()))
        batch_op.add_column(sa.Column("financial_report_id", sa.Integer()))
        batch_op.add_column(sa.Column("financial_source_url", sa.Text()))
        batch_op.create_index(
            "ix_companies_financial_year",
            ["financial_year"],
        )
        batch_op.create_index(
            "ix_companies_annual_revenue",
            ["annual_revenue"],
        )
        batch_op.create_index(
            "ix_companies_financials_status",
            ["financials_status"],
        )


def downgrade():
    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_index("ix_companies_financials_status")
        batch_op.drop_index("ix_companies_annual_revenue")
        batch_op.drop_index("ix_companies_financial_year")
        batch_op.drop_column("financial_source_url")
        batch_op.drop_column("financial_report_id")
        batch_op.drop_column("financial_statement_id")
        batch_op.drop_column("ruz_accounting_entity_id")
        batch_op.drop_column("financials_checked_at")
        batch_op.drop_column("financials_status")
        batch_op.drop_column("financial_statement_submitted_on")
        batch_op.drop_column("annual_profit")
        batch_op.drop_column("annual_total_income")
        batch_op.drop_column("annual_revenue")
        batch_op.drop_column("financial_year")
