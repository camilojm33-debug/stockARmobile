"""Seguridad de modulo Mi Empresa: PIN de 4 digitos y control de intentos."""

from __future__ import annotations

from datetime import timedelta

from werkzeug.security import check_password_hash, generate_password_hash


class CompanySecurityService:
    MAX_PIN_ATTEMPTS = 5
    BLOCK_MINUTES = 15

    @staticmethod
    def _normalize_pin(pin: str | None) -> str:
        return "".join(ch for ch in str(pin or "") if ch.isdigit())

    @staticmethod
    def is_valid_pin(pin: str | None) -> bool:
        normalized = CompanySecurityService._normalize_pin(pin)
        return len(normalized) == 4

    @staticmethod
    def verify_pin(company, pin: str | None) -> bool:
        normalized = CompanySecurityService._normalize_pin(pin)
        if not CompanySecurityService.is_valid_pin(normalized):
            return False
        if not company.business_pin_hash:
            return False
        return check_password_hash(company.business_pin_hash, normalized)

    @staticmethod
    def set_pin(company, pin: str | None):
        from app import utcnow

        normalized = CompanySecurityService._normalize_pin(pin)
        if not CompanySecurityService.is_valid_pin(normalized):
            raise ValueError("El PIN debe ser numerico de 4 digitos.")
        company.business_pin_hash = generate_password_hash(normalized)
        company.business_pin_failed_attempts = 0
        company.business_pin_blocked_until = None
        company.business_pin_updated_at = utcnow()

    @staticmethod
    def remaining_block_seconds(company, now=None) -> int:
        from app import utcnow

        now = now or utcnow()
        blocked_until = getattr(company, "business_pin_blocked_until", None)
        if not blocked_until or blocked_until <= now:
            return 0
        return int((blocked_until - now).total_seconds())

    @staticmethod
    def reset_attempts(company):
        company.business_pin_failed_attempts = 0
        company.business_pin_blocked_until = None

    @staticmethod
    def register_failed_attempt(company, *, now=None, max_attempts=None, block_minutes=None):
        from app import utcnow

        now = now or utcnow()
        max_attempts = max_attempts or CompanySecurityService.MAX_PIN_ATTEMPTS
        block_minutes = block_minutes or CompanySecurityService.BLOCK_MINUTES

        current_attempts = int(getattr(company, "business_pin_failed_attempts", 0) or 0) + 1
        company.business_pin_failed_attempts = current_attempts
        just_blocked = False
        if current_attempts >= max_attempts:
            company.business_pin_failed_attempts = 0
            company.business_pin_blocked_until = now + timedelta(minutes=block_minutes)
            just_blocked = True
        return current_attempts, just_blocked

