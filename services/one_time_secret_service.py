"""Short-lived server-side secrets for one-time administrative reveals.

Raw secret values never enter the Flask client-side session and are encrypted
at rest. The session only carries an opaque random access token.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet


class OneTimeSecretService:
    DEFAULT_TTL_SECONDS = 120

    @staticmethod
    def _now():
        from app import utcnow

        return utcnow()

    @staticmethod
    def _hash_access_token(raw_token: str) -> str:
        return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _fernet() -> Fernet:
        from flask import current_app

        secret_key = current_app.secret_key or current_app.config.get("SECRET_KEY")
        if not secret_key:
            raise RuntimeError("SECRET_KEY es obligatorio para almacenar secretos temporales.")
        digest = hashlib.sha256(str(secret_key).encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    @classmethod
    def issue(
        cls,
        db_session,
        *,
        user_id: int,
        purpose: str,
        subject_type: str,
        subject_id: int | None,
        secret_value: str,
        ttl_seconds: int | None = None,
    ):
        from app import OneTimeSecret

        raw_access_token = secrets.token_urlsafe(32)
        ttl = max(30, int(ttl_seconds or cls.DEFAULT_TTL_SECONDS))
        row = OneTimeSecret(
            user_id=user_id,
            purpose=purpose,
            subject_type=subject_type,
            subject_id=subject_id,
            access_token_hash=cls._hash_access_token(raw_access_token),
            ciphertext=cls._fernet().encrypt((secret_value or "").encode("utf-8")).decode("ascii"),
            expires_at=cls._now() + timedelta(seconds=ttl),
        )
        db_session.add(row)
        db_session.flush()
        return row, raw_access_token

    @classmethod
    def consume(
        cls,
        db_session,
        *,
        user_id: int,
        purpose: str,
        subject_type: str,
        subject_id: int | None,
        access_token: str | None,
    ):
        from app import OneTimeSecret

        if not access_token:
            return None

        row = (
            OneTimeSecret.query.filter_by(
                user_id=user_id,
                purpose=purpose,
                subject_type=subject_type,
                subject_id=subject_id,
                access_token_hash=cls._hash_access_token(access_token),
            )
            .filter(
                OneTimeSecret.consumed_at.is_(None),
                OneTimeSecret.expires_at > cls._now(),
            )
            .first()
        )
        if row is None:
            return None

        try:
            secret_value = cls._fernet().decrypt(row.ciphertext.encode("ascii")).decode("utf-8")
        except Exception:
            return None

        now = cls._now()
        consumed = (
            db_session.query(OneTimeSecret)
            .filter(
                OneTimeSecret.id == row.id,
                OneTimeSecret.consumed_at.is_(None),
                OneTimeSecret.expires_at > now,
            )
            .update({OneTimeSecret.consumed_at: now}, synchronize_session=False)
        )
        if consumed != 1:
            return None

        db_session.flush()
        return secret_value

    @classmethod
    def revoke(cls, db_session, *, user_id: int, purpose: str, subject_type: str, subject_id: int | None):
        from app import OneTimeSecret

        now = cls._now()
        (
            OneTimeSecret.query.filter_by(
                user_id=user_id,
                purpose=purpose,
                subject_type=subject_type,
                subject_id=subject_id,
            )
            .filter(OneTimeSecret.consumed_at.is_(None))
            .update({OneTimeSecret.consumed_at: now}, synchronize_session=False)
        )
