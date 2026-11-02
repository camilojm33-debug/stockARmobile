"""Invite and acceptance lifecycle for multi-company user memberships."""
from __future__ import annotations

import hashlib
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from flask import current_app, url_for


class CompanyMembershipInvitationService:
    TTL_DAYS = 7

    @staticmethod
    def _now():
        from app import utcnow
        return utcnow()

    @staticmethod
    def _hash(raw_token: str) -> str:
        return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()

    @classmethod
    def create_or_refresh(cls, db_session, *, user, company, role: str, permissions_json: str = "[]"):
        from stockarmobile.company_memberships import ensure_membership

        now = cls._now()
        membership = ensure_membership(
            db_session,
            user,
            company.id,
            role=role,
            permissions_json=permissions_json,
            active=False,
        )
        raw_token = secrets.token_urlsafe(48)
        membership.invitation_token_hash = cls._hash(raw_token)
        membership.invitation_expires_at = now + timedelta(days=cls.TTL_DAYS)
        membership.invited_at = now
        membership.accepted_at = None
        membership.active = False
        db_session.flush()
        invitation_url = url_for(
            "auth.accept_company_invitation",
            token=raw_token,
            _external=True,
        )
        email_sent = cls.send_invitation_email(
            to_email=user.email,
            company_name=company.name,
            role=role,
            invitation_url=invitation_url,
        )
        if current_app.testing:
            current_app.config["_LAST_COMPANY_INVITATION_TOKEN"] = raw_token
            current_app.config["_LAST_COMPANY_INVITATION_USER_ID"] = user.id
            current_app.config["_LAST_COMPANY_INVITATION_COMPANY_ID"] = company.id
        return membership, invitation_url, email_sent

    @classmethod
    def get_valid(cls, raw_token: str):
        from app import UserCompanyMembership

        return (
            UserCompanyMembership.query
            .filter_by(invitation_token_hash=cls._hash(raw_token), active=False)
            .filter(UserCompanyMembership.invitation_expires_at > cls._now())
            .first()
        )

    @staticmethod
    def send_invitation_email(*, to_email: str, company_name: str, role: str, invitation_url: str):
        recipient = str(to_email or "").strip().lower()
        if not recipient:
            return False

        message = EmailMessage()
        message["Subject"] = f"Invitación para acceder a {company_name} - StockArMobile"
        message["From"] = current_app.config.get("SMTP_FROM_EMAIL") or "no-reply@stockarmobile.com"
        message["To"] = recipient
        message.set_content(
            (
                f"Te invitaron a trabajar en {company_name} desde StockArMobile.\n\n"
                f"Rol asignado: {'Administrador' if role == 'admin' else 'Empleado'}.\n\n"
                "Para aceptar la invitación, iniciá sesión con tu cuenta de StockArMobile "
                "y abrí el siguiente enlace:\n"
                f"{invitation_url}\n\n"
                "El enlace vence en 7 días. Si no esperabas esta invitación, podés ignorar este correo."
            )
        )

        smtp_host = (current_app.config.get("SMTP_HOST") or "").strip()
        smtp_user = (current_app.config.get("SMTP_USER") or "").strip()
        smtp_password = current_app.config.get("SMTP_PASSWORD") or ""
        smtp_port = int(current_app.config.get("SMTP_PORT") or 587)
        smtp_use_tls = bool(current_app.config.get("SMTP_USE_TLS", True))
        if not smtp_host:
            current_app.logger.info(
                "Company membership invitation email not sent because SMTP is not configured. to=%s",
                recipient,
            )
            return False

        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
                smtp.ehlo()
                if smtp_use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if smtp_user:
                    smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
            return True
        except Exception:
            current_app.logger.exception("No se pudo enviar invitación de empresa a %s", recipient)
            return False
