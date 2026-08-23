"""add campaign landing pages

Revision ID: e42c61a7d8f9
Revises: b37a9d6f4210
Create Date: 2026-08-22 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "e42c61a7d8f9"
down_revision = "b37a9d6f4210"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "landing_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("preview_token", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("hero_image_url", sa.String(length=1000), nullable=True),
        sa.Column("hero_image_alt", sa.String(length=255), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", name="uq_landing_page_campaign"),
        sa.UniqueConstraint("preview_token", name="uq_landing_page_preview_token"),
        sa.UniqueConstraint("slug", name="uq_landing_page_slug"),
    )


def downgrade():
    op.drop_table("landing_pages")
