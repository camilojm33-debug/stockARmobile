"""Validation helpers for sales domain."""

import re
import uuid


class ValidationService:
    @staticmethod
    def is_truthy(value):
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "si", "on"}

    @staticmethod
    def clean_comprobante_type(raw_value):
        allowed = {
            "factura_a",
            "factura_b",
            "factura_c",
            "ticket_fiscal",
            "remito",
            "otro",
        }
        value = (raw_value or "").strip().lower()
        return value if value in allowed else None

    @classmethod
    def resolve_comprobante_payload(cls, data):
        document_type = (data.get("document_type") or "").strip().lower()
        explicit_requires = cls.is_truthy(data.get("requiere_comprobante"))
        explicit_tipo = cls.clean_comprobante_type(data.get("tipo_comprobante"))
        inferred_tipo = cls.clean_comprobante_type(document_type)

        requiere_comprobante = explicit_requires or bool(explicit_tipo) or bool(inferred_tipo)
        tipo_comprobante = explicit_tipo or inferred_tipo
        observacion_comprobante = (data.get("observacion_comprobante") or "").strip()[:255] if requiere_comprobante else None
        return requiere_comprobante, tipo_comprobante, observacion_comprobante

    @staticmethod
    def requires_identified_client(data, requiere_comprobante, tipo_comprobante):
        document_type = (data.get("document_type") or "").strip().lower()
        if document_type in {"factura_a", "factura_b", "factura_c"}:
            return True
        if requiere_comprobante and tipo_comprobante in {"factura_a", "factura_b", "factura_c"}:
            return True
        return False

    @staticmethod
    def sanitize_checkout_token(raw_value):
        token = (raw_value or "").strip()
        if not token:
            return None
        token = token[:64]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", token):
            raise ValueError("Token de checkout inválido.")
        return token

    @staticmethod
    def new_checkout_token():
        return f"chk_{uuid.uuid4()}"

    @staticmethod
    def validate_edit_reason(reason):
        if not (reason or "").strip():
            raise ValueError("Debes indicar el motivo del cambio.")

    @staticmethod
    def validate_edit_lines(new_lines):
        if not new_lines:
            raise ValueError("La venta debe conservar al menos un producto.")

    @staticmethod
    def validate_delete_role(role):
        if role != "admin":
            raise PermissionError("forbidden")

    @staticmethod
    def validate_cancel_status(status):
        current = (status or "").strip().lower()
        if current in {"anulada", "cancelada", "rechazada"}:
            raise ValueError("La venta ya se encuentra anulada.")
