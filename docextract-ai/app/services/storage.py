"""S3 / MinIO storage service (with optional local-filesystem dev fallback)."""
from __future__ import annotations

import io
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("storage")


class LocalStorageService:
    """Dev-mode storage that writes files under ``settings.local_storage_dir``.

    Implements the same surface as :class:`StorageService` so the rest of the
    codebase is identical regardless of backend. Never used in production.
    """

    def __init__(self, base_dir: str | None = None, bucket: str | None = None) -> None:
        self._base = Path(base_dir or settings.local_storage_dir).resolve()
        self._bucket = bucket or settings.s3_bucket
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def bucket(self) -> str:
        return self._bucket

    def _abs(self, key: str) -> Path:
        # Defensive — never let a key escape the storage root
        candidate = (self._base / self._bucket / key).resolve()
        if not str(candidate).startswith(str(self._base)):
            raise ValueError("invalid_storage_key")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def ensure_bucket(self) -> None:
        (self._base / self._bucket).mkdir(parents=True, exist_ok=True)

    def build_key(self, tenant_id: str, filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        return f"tenants/{tenant_id}/documents/{uuid.uuid4()}.{ext}"

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        path = self._abs(key)
        path.write_bytes(data)
        return key

    def upload_stream(self, key: str, fileobj: BinaryIO, content_type: str) -> str:
        path = self._abs(key)
        with path.open("wb") as fh:
            shutil.copyfileobj(fileobj, fh)
        return key

    def download(self, key: str) -> bytes:
        return self._abs(key).read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._abs(key).unlink()
        except FileNotFoundError:
            pass

    def presigned_get(self, key: str, expires: int = 3600) -> str:
        # No URL signing in dev — return a file:// path. Frontend never calls this.
        return f"file://{self._abs(key)}"

    def health(self) -> bool:
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            return os.access(self._base, os.W_OK)
        except Exception:
            return False


class StorageService:
    def __init__(self) -> None:
        import boto3
        from botocore.client import Config

        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
        self._bucket = settings.s3_bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                log.info("bucket_created", bucket=self._bucket)
            except ClientError as exc:
                log.warning("bucket_create_failed", error=str(exc))

    def build_key(self, tenant_id: str, filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        return f"tenants/{tenant_id}/documents/{uuid.uuid4()}.{ext}"

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    def upload_stream(self, key: str, fileobj: BinaryIO, content_type: str) -> str:
        self._client.upload_fileobj(
            fileobj,
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return key

    def download(self, key: str) -> bytes:
        buf = io.BytesIO()
        self._client.download_fileobj(self._bucket, key, buf)
        return buf.getvalue()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def presigned_get(self, key: str, expires: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires,
        )

    def health(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception:
            return False


def _build_storage_service():
    if (settings.storage_mode or "s3").lower() == "local":
        log.info("storage_mode_local", dir=settings.local_storage_dir)
        return LocalStorageService()
    return StorageService()


storage_service = _build_storage_service()
