"""Schemas for the export endpoints."""
from __future__ import annotations

from pydantic import BaseModel


class ExportError(BaseModel):
    status: str = "error"
    error: str
    detail: str = ""
