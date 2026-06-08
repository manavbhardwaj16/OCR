"""Tests for self-service signup + email verification flow."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.api_key import APIKey
from app.models.email_verification import EmailVerification
from app.models.tenant import Tenant
from app.models.user import User


def _signup(client, email=None, company="Acme Co", password="longpass123"):
    return client.post(
        "/api/v1/signup",
        json={
            "company_name": company,
            "email": email or f"u-{secrets.token_hex(3)}@acme.example.com",
            "password": password,
        },
    )


def test_signup_creates_tenant_and_user_without_api_key(client, engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    email = f"u-{secrets.token_hex(3)}@acme.example.com"
    r = _signup(client, email=email)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "tenant_id" in body
    # Unverified accounts have empty api_key_hash on the tenant
    db = Session()
    try:
        tenant = db.get(Tenant, body["tenant_id"])
        assert tenant is not None
        assert tenant.api_key_hash == ""
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        assert user.tenant_id == tenant.id
        verif = db.execute(
            select(EmailVerification).where(EmailVerification.email == email)
        ).scalar_one()
        assert verif.used_at is None
        # No APIKey row yet
        assert db.execute(select(APIKey).where(APIKey.tenant_id == tenant.id)).first() is None
    finally:
        db.close()


def test_signup_duplicate_email_409(client):
    email = f"dup-{secrets.token_hex(3)}@acme.example.com"
    r1 = _signup(client, email=email)
    assert r1.status_code == 201
    r2 = _signup(client, email=email)
    assert r2.status_code == 409


def test_signup_short_password_validation(client):
    r = _signup(client, password="short")
    assert r.status_code == 422


def test_verify_email_provisions_api_key_and_jwt(client, engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    email = f"v-{secrets.token_hex(3)}@acme.example.com"
    r = _signup(client, email=email)
    tenant_id = r.json()["tenant_id"]
    db = Session()
    try:
        token = db.execute(
            select(EmailVerification.token).where(
                EmailVerification.email == email
            )
        ).scalar_one()
    finally:
        db.close()

    v = client.get(f"/api/v1/verify-email?token={token}")
    assert v.status_code == 200, v.text
    payload = v.json()
    assert payload["access_token"]
    assert payload["api_key"].startswith("dx_")
    assert payload["tenant_id"] == tenant_id

    db = Session()
    try:
        tenant = db.get(Tenant, tenant_id)
        assert tenant.api_key_hash != ""
        key_row = db.execute(
            select(APIKey).where(APIKey.tenant_id == tenant.id)
        ).scalar_one()
        # Feature 4: prefix is populated on key creation
        assert key_row.key_prefix
        assert payload["api_key"].startswith(key_row.key_prefix)
        verif = db.execute(
            select(EmailVerification).where(
                EmailVerification.token == token
            )
        ).scalar_one()
        assert verif.used_at is not None
    finally:
        db.close()

    # JWT works for an authenticated endpoint
    me = client.get(
        "/api/v1/analytics/usage",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert me.status_code == 200


def test_verify_email_token_reuse_410(client):
    email = f"r-{secrets.token_hex(3)}@acme.example.com"
    _signup(client, email=email)
    # Capture token directly via DB
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        token = db.execute(
            select(EmailVerification.token).where(
                EmailVerification.email == email
            )
        ).scalar_one()
    finally:
        db.close()

    r1 = client.get(f"/api/v1/verify-email?token={token}")
    assert r1.status_code == 200
    r2 = client.get(f"/api/v1/verify-email?token={token}")
    assert r2.status_code == 410


def test_verify_email_invalid_token_400(client):
    r = client.get("/api/v1/verify-email?token=not-a-real-token")
    assert r.status_code == 400


def test_verify_email_expired_410(client, engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    email = f"e-{secrets.token_hex(3)}@acme.example.com"
    _signup(client, email=email)
    db = Session()
    try:
        verif = db.execute(
            select(EmailVerification).where(
                EmailVerification.email == email
            )
        ).scalar_one()
        verif.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        token = verif.token
    finally:
        db.close()
    r = client.get(f"/api/v1/verify-email?token={token}")
    assert r.status_code == 410
