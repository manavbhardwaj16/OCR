"""Supervisor entrypoint — mounts the DocExtract AI FastAPI app.

The real application lives in /app/docextract-ai. This shim:
  1. Loads the dev .env (sqlite + local storage fallback)
  2. Adds the docextract-ai package to sys.path
  3. Imports the FastAPI app
  4. Creates DB tables on startup if missing (sqlite dev mode)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load /app/backend/.env BEFORE importing the docextract-ai app so its
# pydantic-settings picks up DATABASE_URL, S3_*, JWT_SECRET, etc.
load_dotenv(Path(__file__).parent / ".env")

DOCEXTRACT_ROOT = Path("/app/docextract-ai")
if str(DOCEXTRACT_ROOT) not in sys.path:
    sys.path.insert(0, str(DOCEXTRACT_ROOT))

# Import the real app
from app.main import app  # noqa: E402  -- import after sys.path mutation
from app.core.database import engine  # noqa: E402
from app.models import Base  # noqa: E402

# Dev-mode bootstrap: ensure schema exists when running on SQLite.
# In production this is handled by alembic migrations.
db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("sqlite"):
    Base.metadata.create_all(engine)
