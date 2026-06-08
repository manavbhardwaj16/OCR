"""Analytics endpoints — customer-facing usage + internal admin dashboards.

Customer:
  GET /api/v1/analytics/usage

Admin (gated by role==ADMIN **and** tenant_id==ADMIN_TENANT_ID env var):
  GET /api/v1/admin/analytics/tenants
  GET /api/v1/admin/analytics/confidence
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Iterable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal
from app.core.config import settings
from app.core.database import get_db
from app.models.document import Document, DocumentStatus
from app.models.extraction import Extraction
from app.models.review import ReviewQueue, ReviewStatus
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.analytics import (
    ConfidenceAnalyticsResponse,
    ConfidenceBucket,
    DailyConfidencePoint,
    DailyVolumePoint,
    FieldBreakdown,
    TenantRow,
    TenantsAnalyticsResponse,
    UsageResponse,
)

router = APIRouter()

PLAN_LIMITS = {
    "free": 500,
    "starter": 2500,
    "pro": 10000,
    "business": 50000,
    "enterprise": 1_000_000,
}

# Field names whose value/confidence we track in field_level_breakdown.
TRACKED_FIELDS = (
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


def _first_of_month(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


# =========================================================================
# Customer: GET /analytics/usage
# =========================================================================

@router.get("/analytics/usage", response_model=UsageResponse)
def usage(
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> UsageResponse:
    now = datetime.now(timezone.utc)
    month_start = _first_of_month(now)
    period = now.strftime("%Y-%m")

    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise HTTPException(404, "tenant_not_found")
    plan = (tenant.plan or "free").lower()
    plan_limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])

    # Extractions this month — group by document_type
    by_type_rows = db.execute(
        select(Extraction.document_type, func.count())
        .where(
            Extraction.tenant_id == principal.tenant_id,
            Extraction.created_at >= month_start,
        )
        .group_by(Extraction.document_type)
    ).all()
    by_type = {(t or "UNKNOWN"): int(c) for t, c in by_type_rows}
    extractions_this_month = sum(by_type.values())

    # Daily volume for last 30 days
    since = now - timedelta(days=30)
    daily_rows = db.execute(
        select(func.date(Extraction.created_at), func.count())
        .where(
            Extraction.tenant_id == principal.tenant_id,
            Extraction.created_at >= since,
        )
        .group_by(func.date(Extraction.created_at))
        .order_by(func.date(Extraction.created_at))
    ).all()
    counts_by_date: dict[str, int] = {}
    for d, c in daily_rows:
        as_date = _to_date(d)
        if as_date is None and isinstance(d, str):
            try:
                as_date = datetime.fromisoformat(d).date()
            except ValueError:
                continue
        if as_date is None:
            continue
        counts_by_date[as_date.isoformat()] = int(c)

    # Fill empty days (so charts get a full 30-day window)
    daily_volume: list[DailyVolumePoint] = []
    for i in range(30, -1, -1):
        d = (now - timedelta(days=i)).date().isoformat()
        daily_volume.append(
            DailyVolumePoint(date=d, count=counts_by_date.get(d, 0))
        )

    # Documents totals
    documents_total = db.execute(
        select(func.count(Document.id)).where(Document.tenant_id == principal.tenant_id)
    ).scalar_one()
    documents_this_month = db.execute(
        select(func.count(Document.id)).where(
            Document.tenant_id == principal.tenant_id,
            Document.created_at >= month_start,
        )
    ).scalar_one()

    # Review queue
    review_pending = db.execute(
        select(func.count(ReviewQueue.id)).where(
            ReviewQueue.tenant_id == principal.tenant_id,
            ReviewQueue.status == ReviewStatus.PENDING,
        )
    ).scalar_one()

    # Average confidence this month
    avg_conf = db.execute(
        select(func.avg(Extraction.overall_confidence)).where(
            Extraction.tenant_id == principal.tenant_id,
            Extraction.created_at >= month_start,
        )
    ).scalar()
    avg_conf_f = float(avg_conf) if avg_conf is not None else 0.0

    pct = (extractions_this_month / plan_limit * 100.0) if plan_limit else 0.0

    return UsageResponse(
        period=period,
        plan=plan,
        plan_limit=plan_limit,
        extractions_this_month=extractions_this_month,
        usage_percentage=round(pct, 2),
        extractions_by_document_type=by_type,
        daily_volume=daily_volume,
        documents_total=int(documents_total or 0),
        documents_this_month=int(documents_this_month or 0),
        review_queue_pending=int(review_pending or 0),
        average_confidence_this_month=round(avg_conf_f, 4),
    )


# =========================================================================
# Admin gating
# =========================================================================

def _require_admin(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Two-factor admin gate: role == ADMIN AND tenant_id == ADMIN_TENANT_ID."""
    admin_tenant_id = (settings.admin_tenant_id or "").strip()
    if not admin_tenant_id:
        raise HTTPException(403, "admin_tenant_id_not_configured")
    if principal.auth_type != "jwt":
        raise HTTPException(403, "admin_routes_require_jwt_auth")
    if principal.role != UserRole.ADMIN:
        raise HTTPException(403, "insufficient_role")
    try:
        if uuid.UUID(admin_tenant_id) != principal.tenant_id:
            raise HTTPException(403, "not_authorized_for_admin")
    except (ValueError, AttributeError):
        raise HTTPException(500, "invalid_admin_tenant_id_configuration") from None
    return principal


# =========================================================================
# Admin: GET /admin/analytics/tenants
# =========================================================================

@router.get(
    "/admin/analytics/tenants", response_model=TenantsAnalyticsResponse
)
def admin_tenants(
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[Principal, Depends(_require_admin)],
) -> TenantsAnalyticsResponse:
    now = datetime.now(timezone.utc)
    month_start = _first_of_month(now)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    tenants = db.execute(select(Tenant).order_by(Tenant.created_at.desc())).scalars().all()

    # Bulk aggregates per tenant — single query for each metric to avoid N+1.
    totals = dict(
        db.execute(
            select(Extraction.tenant_id, func.count())
            .group_by(Extraction.tenant_id)
        ).all()
    )
    this_month = dict(
        db.execute(
            select(Extraction.tenant_id, func.count())
            .where(Extraction.created_at >= month_start)
            .group_by(Extraction.tenant_id)
        ).all()
    )
    last_at = dict(
        db.execute(
            select(Extraction.tenant_id, func.max(Extraction.created_at))
            .group_by(Extraction.tenant_id)
        ).all()
    )
    avg30 = dict(
        db.execute(
            select(
                Extraction.tenant_id,
                func.avg(Extraction.overall_confidence),
            )
            .where(Extraction.created_at >= thirty_days_ago)
            .group_by(Extraction.tenant_id)
        ).all()
    )
    review_pending = dict(
        db.execute(
            select(ReviewQueue.tenant_id, func.count())
            .where(ReviewQueue.status == ReviewStatus.PENDING)
            .group_by(ReviewQueue.tenant_id)
        ).all()
    )

    rows: list[TenantRow] = []
    for t in tenants:
        rows.append(
            TenantRow(
                tenant_id=str(t.id),
                name=t.name,
                plan=t.plan or "free",
                extractions_total=int(totals.get(t.id, 0) or 0),
                extractions_this_month=int(this_month.get(t.id, 0) or 0),
                last_extraction_at=last_at.get(t.id),
                average_confidence_30d=round(float(avg30.get(t.id) or 0.0), 4),
                review_queue_pending=int(review_pending.get(t.id, 0) or 0),
                created_at=t.created_at,
            )
        )

    total_today = db.execute(
        select(func.count(Extraction.id)).where(Extraction.created_at >= today_start)
    ).scalar_one()
    total_this_month = db.execute(
        select(func.count(Extraction.id)).where(Extraction.created_at >= month_start)
    ).scalar_one()

    return TenantsAnalyticsResponse(
        tenants=rows,
        total_tenants=len(rows),
        total_extractions_today=int(total_today or 0),
        total_extractions_this_month=int(total_this_month or 0),
    )


# =========================================================================
# Admin: GET /admin/analytics/confidence
# =========================================================================

def _percentile_pure_python(values: Iterable[float], pct: float) -> float:
    """Linear-interpolation percentile. Used as a SQLite fallback because
    PERCENTILE_CONT is Postgres-only."""
    arr = sorted(float(v) for v in values if v is not None)
    if not arr:
        return 0.0
    if len(arr) == 1:
        return arr[0]
    k = (len(arr) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(arr) - 1)
    frac = k - lo
    return arr[lo] + (arr[hi] - arr[lo]) * frac


def _confidence_buckets_postgres(db: Session) -> dict[str, ConfidenceBucket]:
    # PERCENTILE_CONT — Postgres only
    rows = db.execute(
        select(
            Extraction.document_type,
            func.percentile_cont(0.5).within_group(Extraction.overall_confidence),
            func.percentile_cont(0.1).within_group(Extraction.overall_confidence),
            func.percentile_cont(0.9).within_group(Extraction.overall_confidence),
            func.count(),
        ).group_by(Extraction.document_type)
    ).all()
    out: dict[str, ConfidenceBucket] = {}
    for doc_type, p50, p10, p90, count in rows:
        out[doc_type or "UNKNOWN"] = ConfidenceBucket(
            median=round(float(p50 or 0.0), 4),
            p10=round(float(p10 or 0.0), 4),
            p90=round(float(p90 or 0.0), 4),
            count=int(count or 0),
        )
    return out


def _confidence_buckets_python(db: Session) -> dict[str, ConfidenceBucket]:
    rows = db.execute(
        select(Extraction.document_type, Extraction.overall_confidence)
    ).all()
    grouped: dict[str, list[float]] = defaultdict(list)
    for doc_type, conf in rows:
        grouped[doc_type or "UNKNOWN"].append(float(conf or 0.0))
    return {
        dt: ConfidenceBucket(
            median=round(_percentile_pure_python(vals, 0.5), 4),
            p10=round(_percentile_pure_python(vals, 0.1), 4),
            p90=round(_percentile_pure_python(vals, 0.9), 4),
            count=len(vals),
        )
        for dt, vals in grouped.items()
    }


def _daily_trend_python(db: Session) -> list[DailyConfidencePoint]:
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    rows = db.execute(
        select(Extraction.created_at, Extraction.overall_confidence).where(
            Extraction.created_at >= thirty_days_ago
        )
    ).all()
    grouped: dict[str, list[float]] = defaultdict(list)
    for created_at, conf in rows:
        d = _to_date(created_at) or datetime.now(timezone.utc).date()
        grouped[d.isoformat()].append(float(conf or 0.0))
    return [
        DailyConfidencePoint(
            date=d,
            median_confidence=round(_percentile_pure_python(vals, 0.5), 4),
            extraction_count=len(vals),
        )
        for d, vals in sorted(grouped.items())
    ]


def _field_breakdown(db: Session) -> dict[str, FieldBreakdown]:
    """Iterate every extracted_json row and aggregate per-field stats.

    Backend-agnostic implementation. For Postgres-only deployments you could
    replace this with a single GROUP BY using ``->>`` and ``jsonb_path_query``,
    but the row count here is naturally bounded by tenants × docs and the
    response is cached at the API layer in production (out of scope for now).
    """
    rows = db.execute(select(Extraction.extracted_json)).all()
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    empties: dict[str, int] = defaultdict(int)
    for (payload,) in rows:
        if not isinstance(payload, dict):
            continue
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            continue
        for field in TRACKED_FIELDS:
            node = data.get(field)
            if not isinstance(node, dict):
                continue
            counts[field] += 1
            sums[field] += float(node.get("confidence") or 0.0)
            if not (node.get("value") or "").strip():
                empties[field] += 1
    return {
        field: FieldBreakdown(
            avg_confidence=round(sums[field] / counts[field], 4) if counts[field] else 0.0,
            empty_rate=round(empties[field] / counts[field], 4) if counts[field] else 0.0,
        )
        for field in TRACKED_FIELDS
        if counts[field] > 0
    }


@router.get(
    "/admin/analytics/confidence", response_model=ConfidenceAnalyticsResponse
)
def admin_confidence(
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[Principal, Depends(_require_admin)],
) -> ConfidenceAnalyticsResponse:
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    try:
        by_type = (
            _confidence_buckets_postgres(db)
            if dialect == "postgresql"
            else _confidence_buckets_python(db)
        )
    except Exception:
        # Fallback if PERCENTILE_CONT fails for any reason
        by_type = _confidence_buckets_python(db)

    return ConfidenceAnalyticsResponse(
        by_document_type=by_type,
        daily_trend_30d=_daily_trend_python(db),
        field_level_breakdown=_field_breakdown(db),
    )


# unused import guard
_ = (case, DocumentStatus)
