"""Diagnóstico de duplicados de Company/Subscription (solo lectura, no borra nada).

Uso:
    & ".venv/Scripts/python.exe" scripts/diagnose_duplicate_subscriptions.py

Detecta:
  - Empresas con más de una Subscription registrada.
  - Cuál suscripción está actualmente activa/vigente (según SubscriptionService).
  - Pagos (Payment) asociados a cada suscripción encontrada.
  - Usuarios asociados a cada Company (para detectar Companies "hermanas" con
    nombre repetido creadas por error).

Este script NO modifica ni elimina ningún registro. Es sólo un reporte para que
un humano decida manualmente cuál conservar antes de correr cualquier limpieza.
"""

from __future__ import annotations

import sys
import argparse
from collections import defaultdict

sys.path.insert(0, ".")

from app import AuditLog, Company, Invoice, Payment, PaymentHistory, Subscription, SubscriptionCommandExecution, User, app, db  # noqa: E402
from services.subscription_service import SubscriptionService  # noqa: E402


def _status_label(subscription, company, now):
    effective = SubscriptionService.get_effective_subscription_status(subscription, company=company, now=now)
    active = effective in SubscriptionService.ACTIVE_STATUSES or effective in SubscriptionService.TRIAL_STATUSES
    return effective, active


def _classification(subscription, *, effective_status, is_current):
    if is_current:
        return "actual/canónica: es la primera suscripción vigente u operativa para la empresa"
    if effective_status in {SubscriptionService.STATE_EXPIRED, SubscriptionService.STATE_TRIAL_EXPIRED}:
        return "histórica: está vencida y existe otra suscripción operativa prioritaria"
    if effective_status in {SubscriptionService.STATE_CANCELLED, SubscriptionService.STATE_SUSPENDED}:
        return "histórica/terminal: cancelada o suspendida y no es la suscripción operativa"
    return "secundaria: requiere revisión manual; no se marca como canónica"


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnóstico read-only de suscripciones duplicadas")
    parser.add_argument("--email", help="Filtrar por email de usuario asociado a la empresa")
    args = parser.parse_args()

    with app.app_context():
        subs_by_company: dict[int, list[Subscription]] = defaultdict(list)
        for sub in Subscription.query.order_by(Subscription.company_id, Subscription.id).all():
            subs_by_company[sub.company_id].append(sub)

        company_ids = set(subs_by_company)
        if args.email:
            email = args.email.strip().lower()
            user_company_ids = {
                user.company_id
                for user in User.query.filter(User.email.ilike(email)).all()
                if user.company_id is not None
            }
            company_company_ids = {
                company.id
                for company in Company.query.filter(Company.contact_email.ilike(email)).all()
            }
            company_ids &= user_company_ids | company_company_ids

        selected_company_ids = [cid for cid in subs_by_company if cid in company_ids]
        duplicated_company_ids = [cid for cid in selected_company_ids if len(subs_by_company[cid]) > 1]

        print("=== Empresas con más de una Subscription ===")
        if not duplicated_company_ids:
            print("Ninguna. No se detectaron duplicados de Subscription por Company.")
            if args.email:
                print(f"Filtro email: {args.email}")
                for company_id in selected_company_ids:
                    company = db.session.get(Company, company_id)
                    print(f"  Empresa encontrada: {company.name if company else company_id} (company_id={company_id}) subscriptions={[s.id for s in subs_by_company[company_id]]}")
        for company_id in duplicated_company_ids:
            company = db.session.get(Company, company_id)
            company_name = company.name if company else f"(Company {company_id} inexistente)"
            subs = subs_by_company[company_id]
            now = __import__("app").utcnow()
            ranked = []
            for sub in subs:
                effective_status, is_active = _status_label(sub, company, now)
                ranked.append((sub, effective_status, is_active))
            current = next((sub for sub, _, is_active in ranked if is_active), None)

            print(f"\nEmpresa: {company_name} (company_id={company_id})")
            print(f"  Subscription IDs encontrados: {[s.id for s in subs]}")
            print(f"  Suscripción actual/canónica sugerida: {current.id if current else 'ninguna'}")
            users = User.query.filter_by(company_id=company_id).all()
            print(f"  Usuarios asociados: {[user.email or user.username for user in users]}")
            for sub, effective_status, is_active in ranked:
                payment_count = Payment.query.filter_by(subscription_id=sub.id).count()
                invoice_count = Invoice.query.filter_by(subscription_id=sub.id).count()
                history_count = PaymentHistory.query.filter_by(subscription_id=sub.id).count()
                audit_count = AuditLog.query.filter_by(entity="subscription", entity_id=sub.id).count()
                command_count = SubscriptionCommandExecution.query.filter_by(subscription_id=sub.id).count()
                marker = " <- ACTUAL/CANÓNICA" if current is not None and sub.id == current.id else " <- HISTÓRICA/SECUNDARIA"
                print(
                    f"    - Subscription {sub.id}: company_id={sub.company_id} plan_id={sub.plan_id} "
                    f"plan={(sub.plan.name if sub.plan else '—')} status={sub.status} effective_status={effective_status} "
                    f"start_date={sub.start_date} starts_at={sub.starts_at} ends_at={sub.ends_at} "
                    f"next_billing_date={sub.next_billing_date} trial_end={sub.trial_end} "
                    f"last_payment_date={sub.last_payment_date} provider=see payments "
                    f"mercadopago_subscription_id={sub.mercadopago_subscription_id} "
                    f"external_reference={sub.external_reference} created_at={sub.created_at} updated_at={sub.updated_at} "
                    f"pagos={payment_count} facturas={invoice_count} payment_history={history_count} "
                    f"audit_logs={audit_count} command_executions={command_count}{marker}"
                )
                print(f"      clasificación: {_classification(sub, effective_status=effective_status, is_current=current is not None and sub.id == current.id)}")
                payments = Payment.query.filter_by(subscription_id=sub.id).order_by(Payment.created_at.asc()).all()
                for payment in payments:
                    print(
                        f"      pago id={payment.id} status={payment.status} provider={payment.provider} "
                        f"external_payment_id={payment.payment_id} external_reference={payment.external_reference} "
                        f"paid_at={payment.paid_at} created_at={payment.created_at}"
                    )

        print("\n=== Empresas con nombre repetido (posibles Companies duplicadas) ===")
        name_groups: dict[str, list[Company]] = defaultdict(list)
        for company in Company.query.order_by(Company.name, Company.id).all():
            name_groups[(company.name or "").strip().lower()].append(company)
        any_name_dupes = False
        for name, companies in name_groups.items():
            if len(companies) > 1:
                any_name_dupes = True
                print(f"\nNombre: {companies[0].name}")
                for company in companies:
                    user_count = User.query.filter_by(company_id=company.id).count()
                    sub_ids = [s.id for s in subs_by_company.get(company.id, [])]
                    print(f"  Company {company.id}: usuarios={user_count} subscriptions={sub_ids} created_at={company.created_at}")
        if not any_name_dupes:
            print("Ninguna. No se detectaron Companies con nombre repetido.")

        print("\nRecordatorio: este reporte es sólo diagnóstico. No borrar registros hasta")
        print("determinar manualmente cuál es el original, cuál es el duplicado, qué pagos")
        print("pertenecen a cada uno y qué usuarios pertenecen a cada empresa.")


if __name__ == "__main__":
    main()
