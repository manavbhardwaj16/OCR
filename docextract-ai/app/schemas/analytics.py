"""Schemas for analytics endpoints (customer + admin)."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


# ---------- Customer-facing ----------

class DailyVolumePoint(BaseModel):
    date: str  # YYYY-MM-DD
    count: int


class UsageResponse(BaseModel):
    period: str  # YYYY-MM
    plan: str
    plan_limit: int
    extractions_this_month: int
    usage_percentage: float
    extractions_by_document_type: Dict[str, int]
    daily_volume: List[DailyVolumePoint]
    documents_total: int
    documents_this_month: int
    review_queue_pending: int
    average_confidence_this_month: float


# ---------- Admin: tenants ----------

class TenantRow(BaseModel):
    tenant_id: str
    name: str
    plan: str
    extractions_total: int
    extractions_this_month: int
    last_extraction_at: Optional[datetime] = None
    average_confidence_30d: float
    review_queue_pending: int
    created_at: datetime


class TenantsAnalyticsResponse(BaseModel):
    tenants: List[TenantRow]
    total_tenants: int
    total_extractions_today: int
    total_extractions_this_month: int


# ---------- Admin: confidence ----------

class ConfidenceBucket(BaseModel):
    median: float
    p10: float
    p90: float
    count: int


class DailyConfidencePoint(BaseModel):
    date: str
    median_confidence: float
    extraction_count: int


class FieldBreakdown(BaseModel):
    avg_confidence: float
    empty_rate: float


class ConfidenceAnalyticsResponse(BaseModel):
    by_document_type: Dict[str, ConfidenceBucket]
    daily_trend_30d: List[DailyConfidencePoint]
    field_level_breakdown: Dict[str, FieldBreakdown]
