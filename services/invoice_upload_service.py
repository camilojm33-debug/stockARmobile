"""Temporary, tenant-scoped invoice upload handling for the AI chat."""

from __future__ import annotations

import os
import hashlib
import uuid
from pathlib import Path
from typing import Any

from flask import current_app
from PIL import Image


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
MAX_INVOICE_SIZE_BYTES = 10 * 1024 * 1024


class InvoiceUploadError(ValueError):
    """Expected validation or storage error for an invoice upload."""


class InvoiceUploadService:
    @staticmethod
    def _directory(company_id: int) -> Path:
        return Path(current_app.instance_path) / "uploads" / "companies" / str(company_id) / "invoices"

    @staticmethod
    def _validate_content(upload, extension: str) -> None:
        upload.stream.seek(0)
        if extension == ".pdf":
            if upload.stream.read(5) != b"%PDF-":
                raise InvoiceUploadError("El archivo no parece ser una imagen/PDF valido.")
            return

        try:
            image = Image.open(upload.stream)
            image.verify()
        except Exception as exc:
            raise InvoiceUploadError("El archivo no parece ser una imagen/PDF valido.") from exc
        finally:
            upload.stream.seek(0)

    @classmethod
    def receive(cls, upload, *, company_id: int, user_id: int, conversation_id: int) -> dict[str, Any]:
        filename = (upload.filename or "").strip()
        extension = Path(filename).suffix.lower()
        if not filename or extension not in ALLOWED_EXTENSIONS:
            raise InvoiceUploadError("Formato de archivo no compatible. Formatos permitidos: JPG, JPEG, PNG, WEBP y PDF.")

        upload.stream.seek(0, os.SEEK_END)
        size = upload.stream.tell()
        upload.stream.seek(0)
        if size <= 0 or size > MAX_INVOICE_SIZE_BYTES:
            if size > MAX_INVOICE_SIZE_BYTES:
                raise InvoiceUploadError("El archivo supera el tamaño máximo permitido.")
            raise InvoiceUploadError("El archivo no parece ser una imagen/PDF valido.")
        cls._validate_content(upload, extension)

        upload_id = uuid.uuid4().hex
        stored_name = f"invoice_{upload_id}{extension}"
        directory = cls._directory(company_id)
        destination = directory / stored_name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            upload.save(destination)
        except OSError as exc:
            current_app.logger.exception("Error almacenando factura temporal: company_id=%s", company_id)
            raise InvoiceUploadError("No se pudo recibir la factura. Intenta nuevamente.") from exc

        return {
            "upload_id": upload_id,
            "original_name": Path(filename).name,
            "stored_name": stored_name,
            "company_id": int(company_id),
            "user_id": int(user_id),
            "conversation_id": int(conversation_id),
            "status": "PENDIENTE_PROCESAMIENTO",
        }

    @classmethod
    def delete(cls, upload_record: dict[str, Any], *, company_id: int) -> bool:
        if int(upload_record.get("company_id") or 0) != int(company_id):
            return False
        stored_name = Path(str(upload_record.get("stored_name") or "")).name
        if not stored_name.startswith("invoice_"):
            return False
        target = cls._directory(company_id) / stored_name
        try:
            target.unlink(missing_ok=True)
            return True
        except OSError:
            current_app.logger.exception("Error eliminando factura temporal: company_id=%s", company_id)
            return False

    @classmethod
    def resolve_path(cls, upload_record: dict[str, Any], *, company_id: int) -> Path:
        if int(upload_record.get("company_id") or 0) != int(company_id):
            raise InvoiceUploadError("La factura no pertenece a esta empresa.")
        stored_name = Path(str(upload_record.get("stored_name") or "")).name
        if not stored_name.startswith("invoice_") or Path(stored_name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise InvoiceUploadError("Referencia de factura inválida.")
        directory = cls._directory(company_id).resolve()
        target = (directory / stored_name).resolve()
        if target.parent != directory or not target.is_file():
            raise InvoiceUploadError("El archivo de factura ya no está disponible.")
        return target

    @classmethod
    def sha256(cls, upload_record: dict[str, Any], *, company_id: int) -> str:
        path = cls.resolve_path(upload_record, company_id=company_id)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
