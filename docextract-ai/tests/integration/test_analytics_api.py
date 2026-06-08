"""Tests for customer + admin analytics endpoints."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.security import (
    api_key_prefix,
    create_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
)
from app.models.api_key import APIKey
from app.models.document import Document, DocumentStatus
from app.models.extraction import Extraction
from app.models.tenant import Tenant
from app.models.user import User, UserRole


def _make_tenant(db, plan="free"):
    raw = generate_api_key()
    tenant = Tenant(
        name=f"T-{secrets.token_hex(3)}",
        api_key_hash=hash_api_key(raw),
        plan=plan,
        rate_limit=60,
    )
    db.add(tenant)
    db.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"u-{secrets.token_hex(3)}@x.example.com",
        password_hash=hash_password("Password!123"),
        role=UserRole.ADMIN,
        jwt_secret=secrets.token_urlsafe(32),
    )
    db.add(user)
    db.add(
        APIKey(
            tenant_id=tenant.id,
            key_hash=hash_api_key(raw),
            key_prefix=api_key_prefix(raw),
            name="k",
            rate_limit_per_minute=60,
        )
    )
    db.flush()
    return tenant, user, raw


def _add_extraction(db, tenant, doc_type="TAX_INVOICE", confidence=0.92, age_days=0):
    doc = Document(
        tenant_id=tenant.id,
        filename=f"f-{secrets.token_hex(2)}.pdf",
        mime_type="application/pdf",
        file_size=1024,
        status=DocumentStatus.COMPLETED,
        s3_key=f"tenants/{tenant.id}/x.pdf",
    )
    db.add(doc)
    db.flush()
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    e = Extraction(
        tenant_id=tenant.id,
        document_id=doc.id,
        document_type=doc_type,
        overall_confidence=confidence,
        extracted_json={
            "document_type": doc_type,
            "overall_confidence": confidence,
            "data": {
                "vendor_name": {"value": "Acme", "confidence": 0.95},
                "vendor_gstin": {"value": "27AABCU9603R1ZX", "confidence": 0.96},
                "grand_total": {"value": "1180.00", "confidence": 0.97},
                "cgst": {"value": "", "confidence": 0.0},
            },
        },
        
        created_at=created,
    )
    db.add(e)
    doc.created_at = created
    db.flush()
    return doc, e


def test_usage_aggregates_by_document_type(client, engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    tenant, user, _ = _make_tenant(db, plan="starter")
    for _ in range(3):
        _add_extraction(db, tenant, doc_type="TAX_INVOICE", confidence=0.95)
    _add_extraction(db, tenant, doc_type="EWAY_BILL", confidence=0.80)
    db.commit()
    jwt = create_access_token(
        subject=str(user.id), tenant_id=str(tenant.id), role="admin"
    )
    db.close()

    r = client.get(
        "/api/v1/analytics/usage", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"] == "starter"
    assert body["plan_limit"] == 2500
    assert body["extractions_this_month"] == 4
    assert body["extractions_by_document_type"]["TAX_INVOICE"] == 3
    assert body["extractions_by_document_type"]["EWAY_BILL"] == 1
    assert len(body["daily_volume"]) == 31  # 30 days + today
    assert 0.0 < body["average_confidence_this_month"] <= 1.0
    assert body["documents_total"] == 4


def test_usage_isolated_per_tenant(client, engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    t1, u1, _ = _make_tenant(db)
    t2, _, _ = _make_tenant(db)
    _add_extraction(db, t1, "TAX_INVOICE")
    _add_extraction(db, t2, "TAX_INVOICE")
    _add_extraction(db, t2, "TAX_INVOICE")
    db.commit()
    jwt = create_access_token(subject=str(u1.id), tenant_id=str(t1.id), role="admin")
    db.close()
    r = client.get(
        "/api/v1/analytics/usage", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200
    assert r.json()["extractions_this_month"] == 1


def test_admin_routes_gated_without_env(client, engine, monkeypatch):
    """When ADMIN_TENANT_ID is empty, admin routes 403."""
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "admin_tenant_id", "")
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    tenant, user, _ = _make_tenant(db)
    db.commit()
    jwt = create_access_token(subject=str(user.id), tenant_id=str(tenant.id), role="admin")
    db.close()
    r = client.get(
        "/api/v1/admin/analytics/tenants",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403


def test_admin_routes_gated_by_admin_tenant_id(client, engine, monkeypatch):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    admin_t, admin_u, _ = _make_tenant(db)
    other_t, other_u, _ = _make_tenant(db)
    _add_extraction(db, admin_t, "TAX_INVOICE")
    _add_extraction(db, other_t, "EWAY_BILL")
    db.commit()
    admin_jwt = create_access_token(
        subject=str(admin_u.id), tenant_id=str(admin_t.id), role="admin"
    )
    other_jwt = create_access_token(
        subject=str(other_u.id), tenant_id=str(other_t.id), role="admin"
    )
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "admin_tenant_id", str(admin_t.id))
    db.close()

    # Admin tenant: 200
    r = client.get(
        "/api/v1/admin/analytics/tenants",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_tenants"] >= 2

    # Other tenant: 403 even with admin role
    r2 = client.get(
        "/api/v1/admin/analytics/tenants",
        headers={"Authorization": f"Bearer {other_jwt}"},
    )
    assert r2.status_code == 403


def test_admin_confidence_endpoint(client, engine, monkeypatch):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    t, u, _ = _make_tenant(db)
    for c in (0.5, 0.7, 0.9, 0.95):
        _add_extraction(db, t, "TAX_INVOICE", confidence=c)
    db.commit()
    jwt = create_access_token(subject=str(u.id), tenant_id=str(t.id), role="admin")
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "admin_tenant_id", str(t.id))
    db.close()

    r = client.get(
        "/api/v1/admin/analytics/confidence",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "TAX_INVOICE" in body["by_document_type"]
    bucket = body["by_document_type"]["TAX_INVOICE"]
    # Admin confidence is global — count may include rows from prior tests in
    # the shared session-scoped SQLite. Just assert at least our 4 are present.
    assert bucket["count"] >= 4
    assert 0.0 <= bucket["median"] <= 1.0
    # field_level_breakdown picks up vendor_gstin (95% conf, 0% empty)
    assert "vendor_gstin" in body["field_level_breakdown"]
    # Field-level breakdown is global across all tenants. Just assert presence
    # and that our cgst-always-empty fixture contributes to the empty_rate
    # (i.e. empty_rate > 0). Don't assert exact ratios — other tests in the
    # session-scoped SQLite engine may have populated cgst.
    assert body["field_level_breakdown"]["vendor_gstin"]["empty_rate"] >= 0.0
    assert body["field_level_breakdown"]["cgst"]["empty_rate"] > 0.0
