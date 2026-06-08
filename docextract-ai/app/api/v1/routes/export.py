"""Export endpoint — download extractions as JSON or CSV.

GET /api/v1/extractions/{document_id}/export?format=csv|json
GET /api/v1/extractions/export?format=bulk_csv&ids=<uuid>,<uuid>,...

Tenant isolation is enforced on every query via principal.tenant_id.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from typing import Annotated, Any, Iterable, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal
from app.core.database import get_db
from app.models.document import Document
from app.models.extraction import Extraction

router = APIRouter()

ALLOWED_FORMATS = {"json", "csv", "bulk_csv"}
BULK_MAX_IDS = 1000

CSV_HEADERS = [
    "document_id",
    "document_type",
    "overall_confidence",
    "vendor_name",
    "vendor_gstin",
    "customer_name",
    "customer_gstin",
    "document_number",
    "document_date",
    "subtotal",
    "cgst",
    "sgst",
    "igst",
    "total_tax",
    "grand_total",
    "item_description",
    "item_hsn",
    "item_qty",
    "item_rate",
    "item_amount",
]

ROOT_FIELDS = (
    "vendor_name",
    "vendor_gstin",
    "customer_name",
    "customer_gstin",
    "document_number",
    "document_date",
    "subtotal",
    "cgst",
    "sgst",
    "igst",
    "total_tax",
    "grand_total",
)


def _fv(node: Any) -> str:
    """Extract the ``value`` from a {value, confidence} field node."""
    if isinstance(node, dict):
        return str(node.get("value", "") or "")
    return ""


def _doc_rows(doc_id: str, extraction: Extraction) -> List[List[str]]:
    """One row per line item (or one row with empty item cols if none)."""
    payload = extraction.extracted_json or {}
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    base = [
        str(doc_id),
        extraction.document_type or "UNKNOWN",
        f"{extraction.overall_confidence:.4f}",
        *[_fv(data.get(f)) for f in ROOT_FIELDS],
    ]
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return [base + ["", "", "", "", ""]]
    rows: List[List[str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            base
            + [
                _fv(item.get("description")),
                _fv(item.get("hsn")),
                _fv(item.get("qty")),
                _fv(item.get("rate")),
                _fv(item.get("amount")),
            ]
        )
    return rows or [base + ["", "", "", "", ""]]


def _csv_stream(rows_iter: Iterable[List[str]]) -> Iterable[bytes]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate(0)
    for row in rows_iter:
        writer.writerow(row)
        chunk = buf.getvalue()
        if chunk:
            yield chunk.encode("utf-8")
            buf.seek(0)
            buf.truncate(0)


def _get_extraction(
    db: Session, document_id: uuid.UUID, tenant_id: uuid.UUID
) -> Extraction:
    doc = db.get(Document, document_id)
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(404, "document_not_found")
    extraction = db.execute(
        select(Extraction).where(Extraction.document_id == doc.id)
    ).scalar_one_or_none()
    if not extraction:
        raise HTTPException(404, "extraction_not_found")
    return extraction


@router.get("/extractions/export")
def bulk_export(
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
    format: str = Query(default="bulk_csv"),
    ids: str = Query(default="", description="Comma-separated document UUIDs"),
):
    """Bulk CSV export. Accepts ``?ids=uuid1,uuid2,...`` (max 1000)."""
    if format != "bulk_csv":
        raise HTTPException(400, "use_/extractions/{document_id}/export_for_single_doc")
    raw_ids = [s.strip() for s in (ids or "").split(",") if s.strip()]
    if not raw_ids:
        raise HTTPException(400, "ids_query_param_required")
    if len(raw_ids) > BULK_MAX_IDS:
        raise HTTPException(400, f"too_many_ids_max_{BULK_MAX_IDS}")
    try:
        parsed = [uuid.UUID(s) for s in raw_ids]
    except ValueError:
        raise HTTPException(400, "invalid_uuid_in_ids") from None

    def gen():
        for did in parsed:
            try:
                extraction = _get_extraction(db, did, principal.tenant_id)
            except HTTPException:
                # Skip silently — bulk export tolerates partial misses
                continue
            for row in _doc_rows(str(did), extraction):
                yield row

    return StreamingResponse(
        _csv_stream(gen()),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="extractions_bulk.csv"'
        },
    )


@router.get("/extractions/{document_id}/export")
def export_single(
    document_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
    format: str = Query(default="json"),
):
    fmt = (format or "json").lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(400, f"invalid_format_must_be_one_of_{sorted(ALLOWED_FORMATS)}")
    if fmt == "bulk_csv":
        raise HTTPException(400, "use_/extractions/export_with_ids_for_bulk_csv")

    extraction = _get_extraction(db, document_id, principal.tenant_id)

    if fmt == "json":
        payload = extraction.extracted_json or {
            "document_type": extraction.document_type,
            "overall_confidence": extraction.overall_confidence,
            "data": {},
        }
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return StreamingResponse(
            iter([body]),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="extraction_{document_id}.json"'
                )
            },
        )

    # CSV (single doc — one row per line item)
    def gen():
        for row in _doc_rows(str(document_id), extraction):
            yield row

    return StreamingResponse(
        _csv_stream(gen()),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="extraction_{document_id}.csv"'
            )
        },
    )
