from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, GUID, TimestampMixin, UUIDPKMixin


class EmailVerification(UUIDPKMixin, TimestampMixin, Base):
    """One-shot email verification token issued at signup.

    The signup flow creates a Tenant + User (unverified) and stores a token
    here. The user clicks the link, the token is looked up, marked ``used_at``,
    and the tenant's API key is then provisioned + JWT issued.
    """

    __tablename__ = "email_verifications"

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
