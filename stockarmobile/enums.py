"""Domain enums with stable string values."""

from enum import StrEnum


class UserRole(StrEnum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    USER = "user"
    SELLER = "seller"


class SaleStatus(StrEnum):
    CONFIRMED = "confirmada"
    CANCELLED = "anulada"


class QuoteStatus(StrEnum):
    BORRADOR = "BORRADOR"
    ENVIADO = "ENVIADO"
    PENDIENTE = "PENDIENTE"
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"
    VENCIDO = "VENCIDO"
    CONVERTIDO = "CONVERTIDO"
    ANULADO = "ANULADO"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    AUTHORIZED = "authorized"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class SubscriptionStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    PENDING_PAYMENT = "pending_payment"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class CashMovementType(StrEnum):
    INCOME = "ingreso"
    EXPENSE = "egreso"
    WITHDRAWAL = "retiro"
    ADJUSTMENT = "ajuste"
