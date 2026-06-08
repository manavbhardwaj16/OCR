"""Unit tests for analytics helper functions."""
from __future__ import annotations

from app.api.v1.routes.analytics import (
    PLAN_LIMITS,
    TRACKED_FIELDS,
    _percentile_pure_python,
    _to_date,
)


def test_plan_limits_contains_expected_plans():
    for plan in ("free", "starter", "pro", "business"):
        assert plan in PLAN_LIMITS
        assert PLAN_LIMITS[plan] > 0


def test_percentile_basic():
    assert _percentile_pure_python([0.5], 0.5) == 0.5
    assert _percentile_pure_python([], 0.5) == 0.0
    # median of [0.0, 0.5, 1.0] = 0.5
    assert _percentile_pure_python([1.0, 0.0, 0.5], 0.5) == 0.5
    # p10 of equally-spaced data is near the minimum
    vals = [0.1 * i for i in range(11)]  # 0.0 to 1.0
    p10 = _percentile_pure_python(vals, 0.1)
    assert 0.05 <= p10 <= 0.15
    p90 = _percentile_pure_python(vals, 0.9)
    assert 0.85 <= p90 <= 0.95


def test_tracked_fields_include_critical_gst_fields():
    for f in ("vendor_gstin", "grand_total", "subtotal", "cgst", "sgst", "igst"):
        assert f in TRACKED_FIELDS


def test_to_date_handles_none():
    assert _to_date(None) is None
