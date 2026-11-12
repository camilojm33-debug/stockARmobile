"""Auditable WhatsApp marketing consent updates for tenant contacts."""

from __future__ import annotations

from stockarmobile.helpers.dates import utcnow_naive


VALID_STATUSES = {"unknown", "opted_in", "opted_out"}


def record_whatsapp_consent(
    client,
    status: str,
    *,
    source: str,
    evidence: str,
    actor_user_id: int | None = None,
) -> None:
    from app import WhatsAppConsentEvent, db

    next_status = str(status or "").strip().lower()
    if next_status not in VALID_STATUSES:
        raise ValueError("El estado del consentimiento de WhatsApp no es válido.")

    event_source = str(source or "").strip()[:40]
    evidence_text = str(evidence or "").strip()
    if next_status == "opted_in" and (not event_source or not evidence_text):
        raise ValueError("Para habilitar campañas de WhatsApp, registrá el origen y la evidencia del consentimiento.")

    previous_status = str(client.whatsapp_marketing_consent or "unknown").strip().lower()
    has_recorded_opt_in = bool(
        client.whatsapp_marketing_consent_recorded_at
        and client.whatsapp_marketing_consent_source
        and client.whatsapp_marketing_consent_evidence
    )
    evidence_changed = (
        next_status == "opted_in"
        and (
            event_source != str(client.whatsapp_marketing_consent_source or "")
            or evidence_text != str(client.whatsapp_marketing_consent_evidence or "")
        )
    )
    if previous_status == next_status and not (
        (next_status == "opted_in" and not has_recorded_opt_in) or evidence_changed
    ):
        return

    occurred_at = utcnow_naive()
    client.whatsapp_marketing_consent = next_status
    client.whatsapp_marketing_consent_recorded_at = occurred_at
    client.whatsapp_marketing_consent_source = event_source or None
    client.whatsapp_marketing_consent_evidence = evidence_text or None
    db.session.add(
        WhatsAppConsentEvent(
            company_id=client.company_id,
            client=client,
            previous_status=previous_status,
            new_status=next_status,
            source=event_source or "unspecified",
            evidence=evidence_text or "No additional evidence supplied.",
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )
    )
