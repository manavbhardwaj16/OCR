import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.core.security import (
    api_key_prefix,
    create_access_token,
    generate_api_key,
    hash_api_key,
    verify_password,
)
from app.models.api_key import APIKey
from app.models.user import User
from app.schemas.auth import (
    APIKeyCreate,
    APIKeyCreated,
    APIKeyOut,
    APIKeyRotated,
    TokenRequest,
    TokenResponse,
)

router = APIRouter()


def _require_jwt(principal: Principal) -> None:
    if principal.auth_type != "jwt":
        raise HTTPException(403, "api_key_cannot_manage_api_keys")


def _to_out(key: APIKey) -> APIKeyOut:
    return APIKeyOut(
        id=str(key.id),
        name=key.name,
        rate_limit_per_minute=key.rate_limit_per_minute,
        last_used=key.last_used,
        revoked_at=key.revoked_at,
        created_at=key.created_at,
    )


@router.post("/token", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(
    request: Request,  # required by slowapi
    payload: TokenRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
    token = create_access_token(
        subject=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role.value,
    )
    return TokenResponse(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@router.post("/api-key", response_model=APIKeyCreated, status_code=201)
def create_api_key(
    payload: APIKeyCreate,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> APIKeyCreated:
    _require_jwt(principal)
    raw = generate_api_key()
    entry = APIKey(
        tenant_id=principal.tenant_id,
        key_hash=hash_api_key(raw),
        key_prefix=api_key_prefix(raw),
        name=payload.name,
        rate_limit_per_minute=payload.rate_limit_per_minute,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return APIKeyCreated(
        id=str(entry.id),
        name=entry.name,
        api_key=raw,
        rate_limit_per_minute=entry.rate_limit_per_minute,
    )


@router.get("/api-keys", response_model=List[APIKeyOut])
def list_api_keys(
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
    include_revoked: bool = False,
) -> List[APIKeyOut]:
    _require_jwt(principal)
    stmt = select(APIKey).where(APIKey.tenant_id == principal.tenant_id)
    if not include_revoked:
        stmt = stmt.where(APIKey.revoked_at.is_(None))
    rows = db.execute(stmt.order_by(APIKey.created_at.desc())).scalars().all()
    return [_to_out(k) for k in rows]


@router.post("/api-keys/{key_id}/rotate", response_model=APIKeyRotated)
def rotate_api_key(
    key_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> APIKeyRotated:
    """Replace the secret of an existing key while preserving id, name, and rate_limit.

    The old plaintext stops working immediately. The new plaintext is shown once.
    """
    _require_jwt(principal)
    key = db.get(APIKey, key_id)
    if not key or key.tenant_id != principal.tenant_id:
        raise HTTPException(404, "api_key_not_found")
    if key.revoked_at is not None:
        raise HTTPException(409, "api_key_revoked")
    raw = generate_api_key()
    key.key_hash = hash_api_key(raw)
    key.key_prefix = api_key_prefix(raw)
    key.last_used = None
    now = datetime.now(timezone.utc)
    db.commit()
    db.refresh(key)
    return APIKeyRotated(
        id=str(key.id),
        name=key.name,
        api_key=raw,
        rate_limit_per_minute=key.rate_limit_per_minute,
        rotated_at=now,
    )


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> None:
    """Soft-revoke a key. Idempotent — re-revoking is a no-op."""
    _require_jwt(principal)
    key = db.get(APIKey, key_id)
    if not key or key.tenant_id != principal.tenant_id:
        raise HTTPException(404, "api_key_not_found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return None


# Static fallback to keep secrets module usage if needed by linters
_ = secrets.token_hex(1)
