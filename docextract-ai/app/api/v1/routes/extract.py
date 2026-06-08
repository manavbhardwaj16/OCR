
import time
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.metrics import CONFIDENCE_HIST, EXTRACTION_LATENCY
from app.core.rate_limit import limiter
from app.models.document import Document, DocumentStatus
from app.models.extraction import Extraction
from app.models.review import ReviewQueue
from app.schemas.extraction import (
    ExtractionData,
    ExtractionResponse,
    ValidationResult,
)
from app.services.extraction import LLMError, extraction_service
from app.services.ocr import ocr_service
from app.services.storage import storage_service
from app.services.validation import (
    is_review_required,
    normalize_extraction,
    validate_extraction,
)

router = APIRouter()
log = get_logger("extract")

ALLOWED_MIME = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
}


def _ensure_supported(mime: str | None, filename: str) -> str:
    mime = (mime or "").lower()
    if mime in ALLOWED_MIME:
        return mime
    # Best-effort from filename
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"unsupported_media: {mime}")


def _field_value(d: dict, key: str) -> str:
    node = d.get(key) if isinstance(d, dict) else None
    if isinstance(node, dict):
        return str(node.get("value", "") or "")
    return ""


@router.post("/extract", response_model=ExtractionResponse)
@limiter.limit(settings.rate_limit_default)
async def extract_document(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
    file: UploadFile = File(...),
    async_processing: bool = Form(default=False, alias="async"),
    webhook_url: str | None = Form(default=None),
) -> ExtractionResponse:
    start = time.perf_counter()

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(400, "empty_file")
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            413, f"file_too_large_max_{settings.max_upload_mb}mb"
        )
    mime_type = _ensure_supported(file.content_type, file.filename or "upload")

    # Persist to S3
    s3_key = storage_service.build_key(str(principal.tenant_id), file.filename or "upload")
    storage_service.upload(s3_key, raw, mime_type)

    doc = Document(
        tenant_id=principal.tenant_id,
        filename=file.filename or "upload",
        s3_key=s3_key,
        file_size=len(raw),
        mime_type=mime_type,
        status=DocumentStatus.PENDING,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    if async_processing:
        # Hand off to Celery; return 202-style payload with document_id only.
        from app.workers.tasks import process_document

        process_document.delay(str(doc.id), webhook_url)
        return ExtractionResponse(
            status="queued",
            document_id=str(doc.id),
            document_type="UNKNOWN",
            overall_confidence=0.0,
            processing_time_ms=int((time.perf_counter() - start) * 1000),
            data=ExtractionData(),
            validation=ValidationResult(),
            review_required=False,
        )

    # Synchronous path
    doc.status = DocumentStatus.PROCESSING
    db.commit()
    try:
        response = await _run_extraction_pipeline(db, doc, raw, mime_type)
    except Exception as exc:
        doc.status = DocumentStatus.FAILED
        db.commit()
        log.error("extract_failed", document_id=str(doc.id), error=str(exc))
        raise HTTPException(500, f"extraction_failed: {exc}") from exc

    elapsed = time.perf_counter() - start
    EXTRACTION_LATENCY.labels(document_type=response.document_type).observe(elapsed)
    CONFIDENCE_HIST.labels(document_type=response.document_type).observe(
        response.overall_confidence
    )
    response.processing_time_ms = int(elapsed * 1000)
    return response


async def _run_extraction_pipeline(
    db: Session, doc: Document, raw: bytes, mime_type: str
) -> ExtractionResponse:
    """Shared sync pipeline; also used by Celery worker."""
    ocr_text = ocr_service.extract_text(raw, mime_type)

    try:
        extracted = await extraction_service.extract(ocr_text, hints=doc.filename)
    except LLMError as exc:
        raise RuntimeError(f"llm_extraction_failed: {exc}") from exc

    data_node = extracted.get("data", {}) if isinstance(extracted, dict) else {}
    document_type = str(extracted.get("document_type", "UNKNOWN") or "UNKNOWN").upper()
    overall_confidence = float(extracted.get("overall_confidence", 0.0) or 0.0)

    validation = validate_extraction(
        data_node, db=db, tenant_id=str(doc.tenant_id)
    )

    # Self-correction pass on amount mismatch
    if not validation.amounts_reconciled:
        try:
            corrected = await extraction_service.correct(ocr_text, validation.errors)
            corrected_data = corrected.get("data", {}) if isinstance(corrected, dict) else {}
            new_validation = validate_extraction(
                corrected_data, db=db, tenant_id=str(doc.tenant_id)
            )
            if new_validation.amounts_reconciled:
                data_node = corrected_data
                validation = new_validation
                document_type = str(
                    corrected.get("document_type", document_type) or document_type
                ).upper()
                overall_confidence = float(
                    corrected.get("overall_confidence", overall_confidence)
                )
        except LLMError as exc:
            log.warning("correction_pass_failed", error=str(exc))

    review_required, reason = is_review_required(validation, overall_confidence)

    extraction = Extraction(
        document_id=doc.id,
        tenant_id=doc.tenant_id,
        document_type=document_type,
        overall_confidence=overall_confidence,
        raw_ocr_text=ocr_text,
        extracted_json={
            "document_type": document_type,
            "overall_confidence": overall_confidence,
            "data": data_node,
        },
        validation_result=validation.model_dump(),
        processing_time_ms=0,
        document_number=_field_value(data_node, "document_number") or None,
        vendor_gstin=_field_value(data_node, "vendor_gstin") or None,
        document_date=_field_value(data_node, "document_date") or None,
    )
    db.add(extraction)
    db.flush()

    doc.status = (
        DocumentStatus.NEEDS_REVIEW if review_required else DocumentStatus.COMPLETED
    )

    if review_required:
        db.add(
            ReviewQueue(
                extraction_id=extraction.id,
                tenant_id=doc.tenant_id,
                reason=reason or "review_required",
            )
        )
    db.commit()
    db.refresh(extraction)

    return ExtractionResponse(
        status="success",
        document_id=str(doc.id),
        document_type=document_type,
        overall_confidence=overall_confidence,
        processing_time_ms=0,
        data=normalize_extraction(data_node),
        validation=validation,
        review_required=review_required,
    )
