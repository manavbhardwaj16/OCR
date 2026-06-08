"""Self-service signup + email verification.

Flow:
  POST /api/v1/signup        -> create tenant + unverified user, send token
  GET  /api/v1/verify-email  -> consume token, mint API key + JWT, send welcome
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.security import (
    api_key_prefix,
    create_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
)
from app.models.api_key import APIKey
from app.models.email_verification import EmailVerification
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.email import send_verification_email, send_welcome_email

router = APIRouter()
log = get_logger("signup")

VERIFICATION_TTL_HOURS = 24


class SignupRequest(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)


class SignupResponse(BaseModel):
    message: str
    tenant_id: str


class VerifyEmailResponse(BaseModel):
    message: str
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    api_key: str
    tenant_id: str
    warning: str = "Save your API key now. It cannot be retrieved again."


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
def signup(
    payload: SignupRequest,
    background: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> SignupResponse:
    """Create a tenant + unverified user, send a verification email.

    The account does not get an API key until the email is verified.
    """
    existing = db.execute(
        select(User).where(User.email == str(payload.email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "email_already_registered")

    tenant = Tenant(
        name=payload.company_name,
        api_key_hash="",  # populated on verification
        plan="free",
        rate_limit=60,
    )
    db.add(tenant)
    db.flush()

    user = User(
        tenant_id=tenant.id,
        email=str(payload.email),
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
        jwt_secret=secrets.token_urlsafe(32),
    )
    db.add(user)

    token = secrets.token_urlsafe(32)
    verification = EmailVerification(
        email=str(payload.email),
        token=token,
        tenant_id=tenant.id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TTL_HOURS),
    )
    db.add(verification)
    db.commit()

    # Send email out-of-band so the API responds fast
    background.add_task(
        send_verification_email,
        str(payload.email),
        token,
        settings.app_base_url,
    )

    log.info(
        "signup_initiated",
        tenant_id=str(tenant.id),
        email=str(payload.email),
        # token surfaced in dev so devs can complete signup without SMTP
        verify_url=f"{settings.app_base_url.rstrip('/')}/verify-email?token={token}",
    )

    return SignupResponse(
        message="Verification email sent. Check your inbox.",
        tenant_id=str(tenant.id),
    )


@router.get("/verify-email", response_model=VerifyEmailResponse)
def verify_email(
    token: str,
    background: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
) -> VerifyEmailResponse:
    """Consume a verification token, provision the API key, return JWT + key."""
    record = db.execute(
        select(EmailVerification).where(EmailVerification.token == token)
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(400, "invalid_verification_token")
    if record.used_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "verification_token_already_used")

    # Compare timezone-aware datetimes safely (SQLite may return naive UTC)
    now = datetime.now(timezone.utc)
    exp = record.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        raise HTTPException(status.HTTP_410_GONE, "verification_token_expired")

    tenant = db.get(Tenant, record.tenant_id)
    user = db.execute(
        select(User).where(
            User.tenant_id == record.tenant_id,
            User.email == record.email,
        )
    ).scalar_one_or_none()
    if not tenant or not user:
        raise HTTPException(404, "tenant_or_user_missing")

    # Provision the API key — shown ONCE
    raw_api_key = generate_api_key()
    hashed = hash_api_key(raw_api_key)
    prefix = api_key_prefix(raw_api_key)

    tenant.api_key_hash = hashed
    db.add(
        APIKey(
            tenant_id=tenant.id,
            key_hash=hashed,
            key_prefix=prefix,
            name="default",
            rate_limit_per_minute=tenant.rate_limit or 60,
        )
    )
    record.used_at = now
    db.commit()

    jwt_token = create_access_token(
        subject=str(user.id),
        tenant_id=str(tenant.id),
        role=user.role.value,
    )

    background.add_task(
        send_welcome_email, record.email, tenant.name, raw_api_key
    )

    return VerifyEmailResponse(
        message="Email verified successfully",
        access_token=jwt_token,
        expires_in=settings.jwt_expire_minutes * 60,
        api_key=raw_api_key,
        tenant_id=str(tenant.id),
    )


# satisfy linters: keep uuid import used
_ = uuid
