"""Gmail integration for the StockArMobile commercial acquisition mailbox."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
COMMERCIAL_EMAIL = "stockarmobile@gmail.com"


def _settings():
    return {
        "client_id": os.getenv("GOOGLE_GMAIL_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GOOGLE_GMAIL_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv(
            "GOOGLE_GMAIL_REDIRECT_URI",
            "https://www.stockarmobile.com/superadmin/crm/email/gmail/callback",
        ).strip(),
        "refresh_token": os.getenv("GMAIL_COMMERCIAL_REFRESH_TOKEN", "").strip(),
        "topic_name": os.getenv("GMAIL_COMMERCIAL_PUBSUB_TOPIC", "").strip(),
        "webhook_secret": os.getenv("GMAIL_COMMERCIAL_PUBSUB_SECRET", "").strip(),
        "mailbox": os.getenv("GMAIL_COMMERCIAL_MAILBOX", COMMERCIAL_EMAIL).strip().lower(),
    }


def _require_client():
    settings = _settings()
    if not settings["client_id"] or not settings["client_secret"]:
        raise RuntimeError("Faltan GOOGLE_GMAIL_CLIENT_ID y GOOGLE_GMAIL_CLIENT_SECRET.")
    return settings


def _credentials(refresh_token: str | None = None):
    from google.oauth2.credentials import Credentials

    settings = _require_client()
    token = (refresh_token or settings["refresh_token"]).strip()
    if not token:
        raise RuntimeError("Falta GMAIL_COMMERCIAL_REFRESH_TOKEN.")
    return Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings["client_id"],
        client_secret=settings["client_secret"],
        scopes=[GMAIL_SCOPE],
    )


def gmail_service():
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    credentials = _credentials()
    credentials.refresh(Request())
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def authorization_url(state: str):
    from google_auth_oauthlib.flow import Flow

    settings = _require_client()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings["redirect_uri"]],
            }
        },
        scopes=[GMAIL_SCOPE],
        state=state,
    )
    flow.redirect_uri = settings["redirect_uri"]
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url


def exchange_code(code: str):
    from google_auth_oauthlib.flow import Flow

    settings = _require_client()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings["redirect_uri"]],
            }
        },
        scopes=[GMAIL_SCOPE],
        state=None,
    )
    flow.redirect_uri = settings["redirect_uri"]
    flow.fetch_token(code=code)
    if not flow.credentials.refresh_token:
        raise RuntimeError("Google no devolvió refresh_token. Volvé a autorizar con consentimiento.")
    return flow.credentials.refresh_token


def renew_watch() -> dict:
    settings = _settings()
    if not settings["refresh_token"] or not settings["topic_name"]:
        return {"status": "skipped", "reason": "Gmail watch no configurado."}
    service = gmail_service()
    response = service.users().watch(
        userId="me",
        body={
            "topicName": settings["topic_name"],
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        },
    ).execute()
    return {
        "status": "renewed",
        "history_id": response.get("historyId"),
        "expiration": response.get("expiration"),
    }


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _headers(message: dict) -> dict[str, str]:
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in (message.get("payload", {}).get("headers") or [])
        if item.get("name")
    }


def _extract_text(payload: dict) -> tuple[str, str]:
    plain = []
    html = []

    def walk(part: dict):
        mime = str(part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if data:
            decoded = _decode_body(data)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return "\n".join(plain).strip(), "\n".join(html).strip()


def _message_date(message: dict) -> datetime:
    internal_ms = int(message.get("internalDate") or 0)
    if internal_ms:
        return datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
    return datetime.now(timezone.utc)


def sync_recent_messages(hours: int = 48) -> dict:
    from services.saas_commercial_service import capture_inbound_email

    service = gmail_service()
    settings = _settings()
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"in:inbox to:{settings['mailbox']} after:{cutoff}"
    response = service.users().messages().list(
        userId="me", q=query, maxResults=100
    ).execute()

    received = 0
    duplicates = 0
    skipped = 0
    for item in response.get("messages") or []:
        message = service.users().messages().get(
            userId="me", id=item["id"], format="full"
        ).execute()
        headers = _headers(message)
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        sender_email = sender_email.strip().lower()
        if not sender_email or sender_email == settings["mailbox"]:
            skipped += 1
            continue

        plain, html = _extract_text(message.get("payload") or {})
        result = capture_inbound_email(
            sender_email=sender_email,
            subject=headers.get("subject", ""),
            text=plain,
            html=html,
            external_message_id=headers.get("message-id") or message.get("id", ""),
            recipient_email=headers.get("to", settings["mailbox"]),
        )
        if result.get("status") == "duplicate":
            duplicates += 1
        else:
            received += 1

    return {
        "status": "synced",
        "received": received,
        "duplicates": duplicates,
        "skipped": skipped,
        "checked_hours": hours,
    }


def process_pubsub_notification(payload: dict) -> dict:
    settings = _settings()
    message = payload.get("message") or {}
    encoded = message.get("data") or ""
    if encoded:
        decoded = json.loads(_decode_body(encoded))
        email_address = str(decoded.get("emailAddress") or "").strip().lower()
        if email_address and email_address != settings["mailbox"]:
            return {"status": "ignored", "email_address": email_address}
    return sync_recent_messages(hours=48)
