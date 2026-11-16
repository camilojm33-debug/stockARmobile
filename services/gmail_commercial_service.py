"""Gmail integration for the StockArMobile commercial acquisition mailbox."""
from __future__ import annotations

import base64
import logging
import threading
import time
import json
import os
import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
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
        "pubsub_service_account": os.getenv("GMAIL_COMMERCIAL_PUBSUB_SERVICE_ACCOUNT", "").strip().lower(),
        "pubsub_audience": os.getenv("GMAIL_COMMERCIAL_PUBSUB_AUDIENCE", "https://www.stockarmobile.com/superadmin/crm/email/gmail/pubsub").strip(),
        "mailbox": os.getenv("GMAIL_COMMERCIAL_MAILBOX", COMMERCIAL_EMAIL).strip().lower(),
    }


def _require_client():
    settings = _settings()
    if not settings["client_id"] or not settings["client_secret"]:
        raise RuntimeError("Faltan GOOGLE_GMAIL_CLIENT_ID y GOOGLE_GMAIL_CLIENT_SECRET.")
    return settings


def commercial_gmail_configuration_status() -> dict:
    """Return non-secret readiness signals for the SuperAdmin commercial mailbox."""
    settings = _settings()
    oauth_ready = bool(settings["client_id"] and settings["client_secret"])
    refresh_ready = bool(settings["refresh_token"])
    topic_ready = bool(settings["topic_name"])
    pubsub_identity_ready = bool(settings["pubsub_service_account"])
    watch_ready = oauth_ready and refresh_ready and topic_ready
    send_scope_confirmed = os.getenv("GMAIL_COMMERCIAL_SEND_SCOPE_CONFIRMED", "").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "oauth_configured": oauth_ready,
        "refresh_token_configured": refresh_ready,
        "watch_configured": watch_ready,
        "pubsub_auth_configured": pubsub_identity_ready,
        "pubsub_configured": watch_ready and pubsub_identity_ready,
        "send_scope_confirmed": send_scope_confirmed,
        "send_configured": oauth_ready and refresh_ready and send_scope_confirmed,
        "mailbox": settings["mailbox"],
        "audience": settings["pubsub_audience"],
    }


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
        scopes=GMAIL_SCOPES,
    )


def gmail_service():
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    credentials = _credentials()
    credentials.refresh(Request())
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def authorization_url(state: str, code_verifier: str):
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
        scopes=GMAIL_SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = settings["redirect_uri"]
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url


def exchange_code(code: str, code_verifier: str):
    from google_auth_oauthlib.flow import Flow

    settings = _require_client()
    verifier = str(code_verifier or "").strip()
    if not verifier:
        raise RuntimeError("Falta el code_verifier de la autorización OAuth.")
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
        scopes=GMAIL_SCOPES,
        state=None,
        code_verifier=verifier,
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = settings["redirect_uri"]
    flow.fetch_token(code=code, code_verifier=verifier)
    if not flow.credentials.refresh_token:
        raise RuntimeError("Google no devolvió refresh_token. Volvé a autorizar con consentimiento.")
    return flow.credentials.refresh_token

def renew_watch() -> dict:
    settings = _settings()
    status = commercial_gmail_configuration_status()
    if not status["watch_configured"]:
        return {"status": "skipped", "reason": "Faltan OAuth, refresh token o topic de Gmail."}
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


def sync_recent_messages(hours: int = 48, *, max_pages: int = 20) -> dict:
    """Import recent inbound messages, following Gmail pagination up to a safe cap."""
    from services.saas_commercial_service import capture_inbound_email

    service = gmail_service()
    settings = _settings()
    days = max(1, (max(1, int(hours)) + 23) // 24)
    query = f"in:anywhere newer_than:{days}d -from:{settings['mailbox']}"
    page_token = None
    pages = 0
    found = received = duplicates = deleted_ignored = skipped = 0

    while pages < max(1, min(int(max_pages or 20), 100)):
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        items = response.get("messages") or []
        pages += 1
        found += len(items)

        for item in items:
            gmail_message_id = str(item.get("id") or "").strip()
            if not gmail_message_id:
                skipped += 1
                continue
            message = service.users().messages().get(
                userId="me", id=gmail_message_id, format="full"
            ).execute()
            headers = _headers(message)
            _, sender_email = parseaddr(headers.get("from", ""))
            sender_email = sender_email.strip().lower()
            if not sender_email or sender_email == settings["mailbox"]:
                skipped += 1
                continue

            plain, html = _extract_text(message.get("payload") or {})
            rfc_message_id = str(headers.get("message-id") or "").strip()
            result = capture_inbound_email(
                sender_email=sender_email,
                subject=headers.get("subject", ""),
                text=plain,
                html=html,
                external_message_id=rfc_message_id or gmail_message_id,
                recipient_email=headers.get("to", settings["mailbox"]),
                thread_id=str(message.get("threadId") or "").strip(),
                rfc_message_id=rfc_message_id,
                references=str(headers.get("references") or "").strip(),
            )
            if result.get("status") == "duplicate":
                duplicates += 1
            elif result.get("status") == "ignored_deleted":
                deleted_ignored += 1
            else:
                received += 1

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return {
        "status": "synced",
        "received": received,
        "duplicates": duplicates,
        "deleted_ignored": deleted_ignored,
        "skipped": skipped,
        "found": found,
        "pages": pages,
        "has_more": bool(page_token),
        "checked_hours": hours,
        "query": query,
    }


def send_commercial_reply(
    *,
    recipient_email: str,
    subject: str,
    body: str,
    thread_id: str,
    in_reply_to: str = "",
    references: str = "",
) -> dict:
    """Send a Gmail API reply in the original thread using the commercial mailbox."""
    settings = _settings()
    recipient = str(recipient_email or "").strip().lower()
    thread = str(thread_id or "").strip()
    content = str(body or "").strip()
    if not recipient or "@" not in recipient:
        raise ValueError("La conversación no tiene un email de destino válido.")
    if not thread:
        raise ValueError("Gmail no informó el identificador del hilo; sincronizá nuevamente el correo.")
    if not content:
        raise ValueError("La respuesta no puede estar vacía.")
    if len(content) > 20000:
        raise ValueError("La respuesta no puede superar 20.000 caracteres.")
    if not settings["mailbox"]:
        raise RuntimeError("No está configurada la casilla comercial de Gmail.")

    message = EmailMessage()
    message["From"] = settings["mailbox"]
    message["To"] = recipient
    original_subject = str(subject or "").strip()[:500] or "(sin asunto)"
    message["Subject"] = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
    if in_reply_to:
        message["In-Reply-To"] = str(in_reply_to).strip()[:998]
    reference_chain = " ".join(
        part for part in (str(references or "").strip(), str(in_reply_to or "").strip()) if part
    )
    if reference_chain:
        message["References"] = reference_chain[-900:]
    message.set_content(content)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    payload = {"raw": raw, "threadId": thread}
    try:
        response = gmail_service().users().messages().send(
            userId="me",
            body=payload,
        ).execute()
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in {401, 403}:
            raise RuntimeError(
                "Google no autorizó el envío. Volvé a conectar Gmail desde el CRM y guardá el nuevo refresh token en Render."
            ) from exc
        raise RuntimeError("No se pudo enviar la respuesta por Gmail. Revisá la conexión de la casilla comercial.") from exc

    sent_id = str(response.get("id") or "").strip()
    if not sent_id:
        raise RuntimeError("Gmail no confirmó el identificador del mensaje enviado.")
    return {
        "message_id": sent_id,
        "thread_id": str(response.get("threadId") or thread),
        "recipient": recipient,
        "subject": message["Subject"],
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
    result = sync_recent_messages(hours=48)
    import logging
    logging.getLogger(__name__).info(
        "Gmail Pub/Sub processed notification: status=%s found=%s received=%s duplicates=%s deleted_ignored=%s skipped=%s",
        result.get("status"),
        result.get("found"),
        result.get("received"),
        result.get("duplicates"),
        result.get("deleted_ignored"),
        result.get("skipped"),
    )
    return result

_GMAIL_WATCH_WORKER_LOCK = threading.Lock()
_GMAIL_WATCH_WORKER_STARTED = False
_GMAIL_WATCH_ADVISORY_LOCK_KEY = 918274646


def start_gmail_watch_maintenance_worker(flask_app, *, initial_delay_seconds: int = 90, interval_seconds: int = 86400) -> bool:
    """Renew Gmail push notifications daily from the web worker; no external cron required."""
    if flask_app.testing or "pytest" in __import__("sys").modules:
        return False

    global _GMAIL_WATCH_WORKER_STARTED
    with _GMAIL_WATCH_WORKER_LOCK:
        if _GMAIL_WATCH_WORKER_STARTED:
            return False
        _GMAIL_WATCH_WORKER_STARTED = True

    initial_delay = max(5, min(int(initial_delay_seconds or 90), 3600))
    interval = max(3600, min(int(interval_seconds or 86400), 86400))
    logger = logging.getLogger(__name__)

    def run_once() -> None:
        from app import db
        try:
            with flask_app.app_context():
                if db.engine.dialect.name == "postgresql":
                    with db.engine.connect() as connection:
                        acquired = bool(
                            connection.execute(
                                __import__("sqlalchemy").text("SELECT pg_try_advisory_lock(:key)"),
                                {"key": _GMAIL_WATCH_ADVISORY_LOCK_KEY},
                            ).scalar()
                        )
                        if not acquired:
                            logger.debug("Gmail watch renewal skipped; another web worker owns the lock.")
                            return
                        try:
                            result = renew_watch()
                        finally:
                            connection.execute(
                                __import__("sqlalchemy").text("SELECT pg_advisory_unlock(:key)"),
                                {"key": _GMAIL_WATCH_ADVISORY_LOCK_KEY},
                            )
                            connection.commit()
                else:
                    result = renew_watch()

                if result.get("status") == "renewed":
                    logger.info(
                        "Gmail commercial watch renewed expiration=%s",
                        result.get("expiration") or "not-reported",
                    )
                else:
                    logger.warning(
                        "Gmail commercial watch not renewed status=%s reason=%s",
                        result.get("status"),
                        result.get("reason"),
                    )
        except Exception:
            logger.exception("Gmail commercial watch renewal failed.")
        finally:
            try:
                db.session.remove()
            except Exception:
                pass

    def worker() -> None:
        time.sleep(initial_delay)
        while True:
            run_once()
            time.sleep(interval)

    thread = threading.Thread(
        target=worker,
        name="stockarmobile-gmail-watch-maintenance",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Gmail commercial watch maintenance worker started (initial_delay=%ss interval=%ss)",
        initial_delay,
        interval,
    )
    return True
