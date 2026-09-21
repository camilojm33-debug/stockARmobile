"""Persistent product image storage and validation."""

from __future__ import annotations

import io
import os
import re
import uuid
from pathlib import Path

from flask import current_app
from PIL import Image, ImageFile, UnidentifiedImageError

MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp)$", re.IGNORECASE)

ImageFile.LOAD_TRUNCATED_IMAGES = False


class ProductImageError(ValueError):
    """Expected validation/storage error for a product image upload."""


def upload_dir() -> Path:
    configured = str(current_app.config.get("PRODUCT_UPLOAD_DIR") or "").strip()
    if configured:
        return Path(configured)
    if os.environ.get("RENDER"):
        return Path("/var/data/stockarmobile/product-images")
    return Path(current_app.static_folder) / "uploads" / "products"


def _read_upload(upload) -> bytes:
    filename = (getattr(upload, "filename", "") or "").strip()
    if not filename:
        raise ProductImageError("No se seleccionó una imagen.")

    extension = Path(filename).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ProductImageError("Formato de imagen no permitido. Usa JPG, JPEG, PNG o WEBP.")

    stream = upload.stream
    stream.seek(0)
    data = stream.read(MAX_IMAGE_SIZE_BYTES + 1)
    stream.seek(0)
    if len(data) > MAX_IMAGE_SIZE_BYTES:
        raise ProductImageError("La imagen supera el tamaño máximo de 5 MB.")
    if not data:
        raise ProductImageError("La imagen está vacía.")
    return data


def _validate_image(data: bytes):
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            detected_format = (image.format or "").upper()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            detected_format = (image.format or detected_format or "").upper()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ProductImageError("El archivo no contiene una imagen válida.") from exc

    if detected_format not in ALLOWED_FORMATS:
        raise ProductImageError("El formato real de la imagen no está permitido.")
    return ALLOWED_FORMATS[detected_format]


def save_product_image(upload) -> str:
    """Validate and persist an image, returning its stable public URL."""
    data = _read_upload(upload)
    suffix, _mime = _validate_image(data)

    directory = upload_dir()
    directory.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{suffix}"
    destination = directory / filename
    destination.write_bytes(data)
    return f"/productos/imagen/{filename}"


def delete_product_image(photo: str | None) -> None:
    """Delete only files created by this service."""
    if not photo:
        return
    filename = str(photo).rsplit("/", 1)[-1]
    if not _FILENAME_RE.fullmatch(filename):
        return
    path = upload_dir() / filename
    try:
        path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.warning("No se pudo eliminar imagen de producto %s", filename)


def resolve_product_image(filename: str) -> tuple[Path, str] | None:
    """Resolve a safe image filename and return path plus MIME type."""
    if not _FILENAME_RE.fullmatch(filename or ""):
        return None
    path = upload_dir() / filename
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[suffix]
    return path, mime
