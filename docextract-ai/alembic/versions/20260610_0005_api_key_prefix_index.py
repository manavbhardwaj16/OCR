"""add api_keys.key_prefix indexed lookup column

Revision ID: 0005_api_key_prefix_index
Revises: 0004_email_verification
Create Date: 2026-06-10 00:00:01.000000

Non-breaking ADD-only migration. The new ``key_prefix`` column is nullable —
existing rows remain NULL until their owners rotate the key (auth still works
for legacy rows via the full-scan slow path in ``deps._resolve_api_key``).
Plaintext prefixes for legacy rows cannot be backfilled here because we only
store bcrypt hashes; the auth path auto-heals on next successful verify.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_api_key_prefix_index"
down_revision = "0004_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("key_prefix", sa.String(length=16), nullable=True),
    )
    # Partial index — Postgres only. On SQLite the test stack just creates the
    # schema via Base.metadata.create_all() and skips this migration entirely.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_api_keys_prefix "
            "ON api_keys (key_prefix) WHERE revoked_at IS NULL"
        )
    else:
        op.create_index("ix_api_keys_prefix", "api_keys", ["key_prefix"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_column("api_keys", "key_prefix")
