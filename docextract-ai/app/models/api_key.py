from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, GUID, TimestampMixin, UUIDPKMixin


class APIKey(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Plaintext prefix of the raw API key (first 16 chars). Indexed for O(1)
    # auth lookups so we only bcrypt-compare a handful of candidate rows. Legacy
    # rows created before this column was added remain NULL and fall back to
    # the full-scan slow path until they are rotated.
    key_prefix: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    tenant = relationship("Tenant", back_populates="api_keys")
