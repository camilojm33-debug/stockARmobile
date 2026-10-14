"""Internal backup maintenance endpoints for Render cron jobs."""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from services.backup_service import BackupService
from stockarmobile.extensions import csrf, db

maintenance_bp = Blueprint("maintenance", __name__)


def _expected_token() -> str:
    return str(os.environ.get("BACKUP_AUTOMATION_TOKEN") or "").strip()


def _authorized() -> bool:
    expected = _expected_token()
    supplied = str(request.headers.get("X-Backup-Automation-Token") or "").strip()
    return bool(expected) and bool(supplied) and hmac.compare_digest(supplied, expected)


def _gmail_watch_expected_token() -> str:
    return str(os.environ.get("GMAIL_WATCH_AUTOMATION_TOKEN") or "").strip()


def _gmail_watch_authorized() -> bool:
    expected = _gmail_watch_expected_token()
    supplied = str(request.headers.get("X-Gmail-Watch-Automation-Token") or "").strip()
    return bool(expected) and bool(supplied) and hmac.compare_digest(supplied, expected)


def _company_id_from_env() -> int | None:
    raw = str(os.environ.get("BACKUP_VERIFY_COMPANY_ID") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@maintenance_bp.post("/internal/maintenance/gmail/watch")
@csrf.exempt
def run_gmail_watch_maintenance():
    if not _gmail_watch_authorized():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    try:
        from services.gmail_commercial_service import renew_watch

        result = renew_watch()
        ok = result.get("status") in {"renewed", "skipped"}
        current_app.logger.info("Automated Gmail watch renewal: %s", result)
        return jsonify({"ok": ok, **result}), 200 if ok else 500
    except Exception as exc:
        current_app.logger.exception("Automated Gmail watch renewal failed: %s", exc)
        return jsonify({"ok": False, "error": "gmail_watch_renewal_failed"}), 500


@maintenance_bp.post("/internal/maintenance/backups/run")
@csrf.exempt
def run_backup_maintenance():
    if not _authorized():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    from app import Company

    results = []
    companies = Company.query.filter_by(active=True).order_by(Company.id.asc()).all()
    today = __import__("datetime").datetime.utcnow().date()
    for company in companies:
        try:
            plan = BackupService._plan_context(company.id)
            existing_backups = BackupService.company_backups(company.id)
            has_usable_backup = any(
                item.status in {"ready", "restored"}
                and bool(str(item.path or "").strip())
                and Path(str(item.path)).is_file()
                for item in existing_backups
            )
            due_today = BackupService.automated_backup_due(
                company.id,
                day_of_month=today.day,
            )
            if not due_today and has_usable_backup:
                results.append({
                    "company_id": company.id,
                    "status": "skipped",
                    "plan": plan["code"],
                    "reason": "not_due_this_month_slot",
                })
                continue

            # Bootstrap the first usable copy immediately, even if the monthly
            # plan slot has passed. Otherwise a new tenant can go weeks without
            # the backup advertised in the plan, and the verifier fails daily.
            trigger_type = (
                "automated_render" if due_today else "automated_render_bootstrap"
            )
            backup, plan = BackupService.create_manual_backup(
                company.id,
                user_id=None,
                trigger_type=trigger_type,
            )
            db.session.commit()
            results.append(
                {
                    "company_id": company.id,
                    "status": "ready",
                    "backup_id": backup.id,
                    "plan": plan["code"],
                    "file_name": backup.file_name,
                }
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception(
                "Automated backup failed company_id=%s: %s",
                company.id,
                exc,
            )
            results.append(
                {
                    "company_id": company.id,
                    "status": "error",
                    "error": str(exc),
                }
            )

    verification = None
    verify_company_id = _company_id_from_env()
    if verify_company_id is not None:
        try:
            backups = BackupService.company_backups(verify_company_id)
            backup = next((item for item in backups if item.status in {"ready", "restored"}), None)
            if backup is None:
                raise FileNotFoundError("No hay backup disponible para verificación.")
            verification = BackupService.verify_restore_round_trip(
                backup,
                expected_company_id=verify_company_id,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception(
                "Backup restore round-trip verification failed company_id=%s: %s",
                verify_company_id,
                exc,
            )
            verification = {
                "valid": False,
                "company_id": verify_company_id,
                "error": str(exc),
            }

    ok = all(item["status"] in {"ready", "skipped"} for item in results) and (
        verification is None or bool(verification.get("valid"))
    )
    return jsonify(
        {
            "ok": ok,
            "companies_processed": len(companies),
            "results": results,
            "restore_verification": verification,
        }
    ), 200 if ok else 500

def _billing_reconciliation_expected_token() -> str:
    return str(os.environ.get("BILLING_RECONCILIATION_TOKEN") or "").strip()


def _billing_reconciliation_authorized() -> bool:
    expected = _billing_reconciliation_expected_token()
    supplied = str(request.headers.get("X-Billing-Reconciliation-Token") or "").strip()
    return bool(expected) and bool(supplied) and hmac.compare_digest(supplied, expected)


@maintenance_bp.post("/internal/maintenance/billing/reconcile")
@csrf.exempt
def run_billing_reconciliation():
    if not _billing_reconciliation_authorized():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    try:
        from services.billing_reconciliation_service import BillingReconciliationService

        result = BillingReconciliationService.run()
        current_app.logger.info(
            "Automated billing reconciliation: scanned=%s repaired=%s ignored=%s errors=%s attention=%s",
            result.get("events_scanned", 0),
            result.get("repaired", 0),
            result.get("ignored", 0),
            result.get("errors", 0),
            result.get("attention", 0),
        )
        return jsonify(result), 200 if result.get("ok") is True else 500
    except Exception as exc:
        current_app.logger.exception(
            "Automated billing reconciliation failed: error_type=%s",
            type(exc).__name__,
        )
        return jsonify({
            "ok": False,
            "events_scanned": 0,
            "repaired": 0,
            "ignored": 0,
            "errors": 1,
            "attention": 0,
            "error": "billing_reconciliation_failed",
        }), 500
