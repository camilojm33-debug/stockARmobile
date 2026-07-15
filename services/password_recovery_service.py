"""Token-based password recovery service shared across user portals."""

from __future__ import annotations

import hashlib
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from flask import current_app, url_for


class PasswordRecoveryService:
    @staticmethod
    def _now():
        from app import utcnow

        return utcnow()

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()

    @classmethod
    def _active_token_rows(cls, db_session, *, user_id: int):
        from app import PasswordResetToken

        now = cls._now()
        return (
            PasswordResetToken.query.filter_by(user_id=user_id)
            .filter(
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.revoked_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .all()
        )

    @classmethod
    def create_reset_token(cls, db_session, *, user):
        from app import PasswordResetToken

        now = cls._now()
        for row in cls._active_token_rows(db_session, user_id=user.id):
            row.revoked_at = now

        raw_token = secrets.token_urlsafe(48)
        ttl_minutes = int(current_app.config.get("PASSWORD_RESET_TOKEN_TTL_MINUTES", 60) or 60)
        row = PasswordResetToken(
            user_id=user.id,
            email=(user.email or "").strip().lower(),
            token_hash=cls._hash_token(raw_token),
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        db_session.add(row)
        db_session.flush()
        if current_app.config.get("TESTING"):
            current_app.config["_LAST_PASSWORD_RESET_TOKEN"] = raw_token
            current_app.config["_LAST_PASSWORD_RESET_USER_ID"] = user.id
        return row, raw_token

    @classmethod
    def request_password_reset(cls, db_session, *, user):
        from app import PasswordRecoveryRequest, db, record_audit

        existing = (
            PasswordRecoveryRequest.query.filter_by(user_id=user.id)
            .filter(PasswordRecoveryRequest.status.in_(["pendiente", "atendida"]))
            .order_by(PasswordRecoveryRequest.requested_at.desc())
            .first()
        )
        if existing is None:
            existing = PasswordRecoveryRequest(
                user_id=user.id,
                company_id=user.company_id,
                email=user.email,
                status="pendiente",
            )
            db_session.add(existing)
            db_session.flush()

        token_row, raw_token = cls.create_reset_token(db_session, user=user)
        reset_url = url_for("auth.reset_password", token=raw_token, _external=True)

        record_audit(
            action="password_recovery_requested",
            entity="password_recovery_request",
            entity_id=existing.id,
            user_id=user.id,
            company_id=user.company_id,
            detail="Solicitud de recuperacion con token seguro.",
        )
        record_audit(
            action="password_reset_token_generated",
            entity="password_reset_token",
            entity_id=token_row.id,
            user_id=user.id,
            company_id=user.company_id,
            detail="Token de recuperacion generado.",
        )
        db.session.flush()

        cls.send_reset_email(to_email=user.email, reset_url=reset_url)
        return existing

    @classmethod
    def send_reset_email(cls, *, to_email: str, reset_url: str):
        to_email = (to_email or "").strip().lower()
        if not to_email:
            return False

        msg = EmailMessage()
        msg["Subject"] = "Recuperacion de contrasena - StockArmobile"
        msg["From"] = current_app.config.get("SMTP_FROM_EMAIL") or "no-reply@stockarmobile.com"
        msg["To"] = to_email
        msg.set_content(
            """
Recibimos una solicitud para restablecer tu contrasena en StockArmobile.

Ingresa al siguiente enlace para definir una nueva contrasena:
{reset_url}

Si no solicitaste este cambio, puedes ignorar este correo.
""".strip().format(reset_url=reset_url)
        )

        smtp_host = (current_app.config.get("SMTP_HOST") or "").strip()
        smtp_user = (current_app.config.get("SMTP_USER") or "").strip()
        smtp_password = current_app.config.get("SMTP_PASSWORD") or ""
        smtp_port = int(current_app.config.get("SMTP_PORT") or 587)
        smtp_use_tls = bool(current_app.config.get("SMTP_USE_TLS", True))

        if not smtp_host:
            current_app.logger.info("Password reset email fallback to logs. to=%s reset_url=%s", to_email, reset_url)
            return False

        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
                smtp.ehlo()
                if smtp_use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if smtp_user:
                    smtp.login(smtp_user, smtp_password)
                smtp.send_message(msg)
            return True
        except Exception as exc:  # pragma: no cover - depends on runtime SMTP
            current_app.logger.exception("No se pudo enviar correo de recuperacion: %s", exc)
            return False

    @classmethod
    def get_valid_token_row(cls, *, raw_token: str):
        from app import PasswordResetToken

        token_hash = cls._hash_token(raw_token)
        now = cls._now()
        return (
            PasswordResetToken.query.filter_by(token_hash=token_hash)
            .filter(
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.revoked_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .first()
        )

    @classmethod
    def consume_token_and_set_password(cls, db_session, *, raw_token: str, new_password: str):
        from app import PasswordRecoveryRequest, User, db, record_audit

        token_row = cls.get_valid_token_row(raw_token=raw_token)
        if token_row is None:
            return None

        user = db.session.get(User, token_row.user_id)
        if user is None or not user.active:
            return None

        user.set_password(new_password)
        user.must_change_password = False
        token_row.used_at = cls._now()

        pending = (
            PasswordRecoveryRequest.query.filter_by(user_id=user.id)
            .filter(PasswordRecoveryRequest.status.in_(["pendiente", "atendida"]))
            .all()
        )
        for item in pending:
            item.status = "cerrada"
            item.processed_at = db.func.now()

        record_audit(
            action="password_reset_completed",
            entity="password_reset_token",
            entity_id=token_row.id,
            user_id=user.id,
            company_id=user.company_id,
            detail="Contrasena actualizada con token de recuperacion.",
        )
        db_session.flush()
        return user
