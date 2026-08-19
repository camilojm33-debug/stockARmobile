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
from collections import defaultdict

sys.path.insert(0, ".")

from app import Company, Payment, Subscription, User, app, db  # noqa: E402
from services.subscription_service import SubscriptionService  # noqa: E402


def main() -> None:
    with app.app_context():
        subs_by_company: dict[int, list[Subscription]] = defaultdict(list)
        for sub in Subscription.query.order_by(Subscription.company_id, Subscription.id).all():
            subs_by_company[sub.company_id].append(sub)

        duplicated_company_ids = [cid for cid, subs in subs_by_company.items() if len(subs) > 1]

        print("=== Empresas con más de una Subscription ===")
        if not duplicated_company_ids:
            print("Ninguna. No se detectaron duplicados de Subscription por Company.")
        for company_id in duplicated_company_ids:
            company = db.session.get(Company, company_id)
            company_name = company.name if company else f"(Company {company_id} inexistente)"
            subs = subs_by_company[company_id]
            active = SubscriptionService.active_subscription_for_company(company_id)

            print(f"\nEmpresa: {company_name} (company_id={company_id})")
            print(f"  Subscription IDs encontrados: {[s.id for s in subs]}")
            print(f"  Suscripción activa/vigente sugerida: {active.id if active else 'ninguna'}")
            for sub in subs:
                payment_count = Payment.query.filter_by(subscription_id=sub.id).count()
                marker = " <- posible duplicado" if active is not None and sub.id != active.id else ""
                print(
                    f"    - Subscription {sub.id}: status={sub.status} plan_id={sub.plan_id} "
                    f"start_date={sub.start_date} next_billing_date={sub.next_billing_date} "
                    f"pagos={payment_count}{marker}"
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
