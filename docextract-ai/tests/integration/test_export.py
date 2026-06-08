"""Tests for the export endpoint (CSV / JSON / bulk_csv)."""
from __future__ import annotations

import json
import secrets
import uuid

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


SAMPLE_EXTRACTED = {
    "document_type": "TAX_INVOICE",
    "overall_confidence": 0.92,
    "data": {
        "vendor_name": {"value": "Acme Industries", "confidence": 0.98},
        "vendor_gstin": {"value": "27AABCU9603R1ZX", "confidence": 0.97},
        "customer_name": {"value": "Beta Traders", "confidence": 0.96},
        "customer_gstin": {"value": "27BBBCE5555F1ZY", "confidence": 0.95},
        "document_number": {"value": "INV-001", "confidence": 0.99},
        "document_date": {"value": "2026-03-14", "confidence": 0.99},
        "subtotal": {"value": "1000.00", "confidence": 0.97},
        "cgst": {"value": "90.00", "confidence": 0.96},
        "sgst": {"value": "90.00", "confidence": 0.96},
        "igst": {"value": "", "confidence": 0.0},
        "total_tax": {"value": "180.00", "confidence": 0.97},
        "grand_total": {"value": "1180.00", "confidence": 0.98},
        "items": [
            {
                "description": {"value": "Widget A", "confidence": 0.95},
                "hsn": {"value": "8517", "confidence": 0.98},
                "qty": {"value": "10", "confidence": 0.99},
                "rate": {"value": "100.00", "confidence": 0.99},
                "amount": {"value": "1000.00", "confidence": 0.97},
            }
        ],
    },
}


@pytest.fixture()
def setup_doc(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    raw_key = generate_api_key()
    tenant = Tenant(
        name=f"X-{secrets.token_hex(3)}",
        api_key_hash=hash_api_key(raw_key),
        plan="pro",
        rate_limit=600,
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
            key_hash=hash_api_key(raw_key),
            key_prefix=api_key_prefix(raw_key),
            name="t",
            rate_limit_per_minute=600,
        )
    )
    doc = Document(
        tenant_id=tenant.id,
        filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=1024,
        status=DocumentStatus.COMPLETED,
        s3_key="tenants/x/invoice.pdf",
    )
    db.add(doc)
    db.flush()
    extraction = Extraction(
        tenant_id=tenant.id,
        document_id=doc.id,
        document_type="TAX_INVOICE",
        overall_confidence=0.92,
        extracted_json=SAMPLE_EXTRACTED,
        
    )
    db.add(extraction)
    db.commit()
    jwt = create_access_token(
        subject=str(user.id), tenant_id=str(tenant.id), role=user.role.value
    )
    out = {
        "tenant_id": str(tenant.id),
        "document_id": str(doc.id),
        "jwt": jwt,
        "api_key": raw_key,
    }
    db.close()
    return out


def test_export_json(client, setup_doc):
    r = client.get(
        f"/api/v1/extractions/{setup_doc['document_id']}/export?format=json",
        headers={"Authorization": f"Bearer {setup_doc['jwt']}"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    body = json.loads(r.content)
    assert body["document_type"] == "TAX_INVOICE"
    assert body["data"]["vendor_gstin"]["value"] == "27AABCU9603R1ZX"


def test_export_csv_single(client, setup_doc):
    r = client.get(
        f"/api/v1/extractions/{setup_doc['document_id']}/export?format=csv",
        headers={"Authorization": f"Bearer {setup_doc['jwt']}"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("document_id,document_type,")
    # one data row (single line item)
    assert len(lines) == 2
    assert "27AABCU9603R1ZX" in lines[1]
    assert "Widget A" in lines[1]


def test_export_invalid_format_400(client, setup_doc):
    r = client.get(
        f"/api/v1/extractions/{setup_doc['document_id']}/export?format=xml",
        headers={"Authorization": f"Bearer {setup_doc['jwt']}"},
    )
    assert r.status_code == 400


def test_export_missing_document_404(client, setup_doc):
    r = client.get(
        f"/api/v1/extractions/{uuid.uuid4()}/export?format=json",
        headers={"Authorization": f"Bearer {setup_doc['jwt']}"},
    )
    assert r.status_code == 404


def test_export_tenant_isolation(client, setup_doc, engine):
    """Different tenant cannot export this doc."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    raw_key = generate_api_key()
    other_t = Tenant(
        name=f"Other-{secrets.token_hex(3)}",
        api_key_hash=hash_api_key(raw_key),
        plan="free",
        rate_limit=60,
    )
    db.add(other_t)
    db.flush()
    other_u = User(
        tenant_id=other_t.id,
        email=f"o-{secrets.token_hex(3)}@x.example.com",
        password_hash=hash_password("Password!123"),
        role=UserRole.ADMIN,
        jwt_secret=secrets.token_urlsafe(32),
    )
    db.add(other_u)
    db.commit()
    other_jwt = create_access_token(
        subject=str(other_u.id), tenant_id=str(other_t.id), role="admin"
    )
    db.close()

    r = client.get(
        f"/api/v1/extractions/{setup_doc['document_id']}/export?format=json",
        headers={"Authorization": f"Bearer {other_jwt}"},
    )
    assert r.status_code == 404


def test_export_bulk_csv(client, setup_doc):
    doc_id = setup_doc["document_id"]
    r = client.get(
        f"/api/v1/extractions/export?format=bulk_csv&ids={doc_id}",
        headers={"Authorization": f"Bearer {setup_doc['jwt']}"},
    )
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("document_id,")
    assert len(lines) >= 2


def test_export_bulk_csv_too_many_ids_400(client, setup_doc):
    ids = ",".join(str(uuid.uuid4()) for _ in range(1001))
    r = client.get(
        f"/api/v1/extractions/export?format=bulk_csv&ids={ids}",
        headers={"Authorization": f"Bearer {setup_doc['jwt']}"},
    )
    assert r.status_code == 400
