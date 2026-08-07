"""Audit helpers for sales workflow."""


class AuditService:
    @staticmethod
    def record_success(record_audit_func, *, sale_id, final_total):
        record_audit_func(action="sale_create", entity="sale", entity_id=sale_id, detail=f"Venta registrada total={final_total}")

    @staticmethod
    def record_error(record_audit_func, *, error):
        record_audit_func(action="sale_error", entity="sale", detail=f"Error al crear venta: {error}")

    @staticmethod
    def record_update(record_audit_func, *, sale_id, reason):
        record_audit_func(
            action="sale_update",
            entity="sale",
            entity_id=sale_id,
            detail=f"Venta editada. Motivo: {reason}",
        )

    @staticmethod
    def record_cancel(record_audit_func, *, sale_id, detail):
        record_audit_func(
            action="sale_cancel",
            entity="sale",
            entity_id=sale_id,
            detail=detail,
        )

    @staticmethod
    def record_delete(record_audit_func, *, sale_id, total_amount):
        record_audit_func(
            action="sale_delete",
            entity="sale",
            entity_id=sale_id,
            detail=f"Venta eliminada y stock restituido. Total={total_amount}",
        )
