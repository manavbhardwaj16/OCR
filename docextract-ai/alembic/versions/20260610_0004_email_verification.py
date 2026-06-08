"""create email_verifications table

Revision ID: 0004_email_verification
Revises: 0003_webhook_deliveries
Create Date: 2026-06-10 00:00:00.000000

Non-breaking ADD-only migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_email_verification"
down_revision = "0003_webhook_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_email_verifications_token",
        "email_verifications",
        ["token"],
        unique=True,
    )
    op.create_index(
        "ix_email_verifications_email", "email_verifications", ["email"]
    )
    op.create_index(
        "ix_email_verifications_tenant_id",
        "email_verifications",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verifications_tenant_id", table_name="email_verifications"
    )
    op.drop_index("ix_email_verifications_email", table_name="email_verifications")
    op.drop_index("ix_email_verifications_token", table_name="email_verifications")
    op.drop_table("email_verifications")
