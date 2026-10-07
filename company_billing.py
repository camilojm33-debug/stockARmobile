    current_subscription = SubscriptionService.active_subscription_for_company(company.id)
    pending_plan = _pending_paid_plan_change(company.id)
    if pending_plan is not None and pending_plan.plan_id != plan.id:
        pending_preview = _persisted_checkout_preview(company, subscription_id=pending_plan.id)
        flash(
            f"Ya hay un cambio pendiente al plan {pending_plan.plan.name if pending_plan.plan else 'seleccionado'}. "
            "Tu plan actual sigue activo hasta que se confirme ese pago.",
            "warning",
        )
        if pending_preview is not None and pending_preview.get("status") == "pending":
            return redirect(url_for(
                "company_billing.subscription_portal",
                checkout="created",
                checkout_subscription_id=pending_plan.id,
                checkout_preference_id=pending_preview.get("preference_id"),
                _anchor="payment-checkout",
            ))
        return redirect(url_for(
            "company_billing.subscription_portal",
            selected_plan_id=pending_plan.plan_id,
            _anchor="planes-disponibles",
        ))

    command_result = None
    subscription = pending_plan if pending_plan is not None else None
    try:
        if subscription is None:
            command_result = SubscriptionService.run_command(
                db.session,
                SubscriptionService.ChangePlanCommand(
                    company_id=company.id,
                    plan_id=plan.id,
                    actor_user_id=current_user.id,
                    actor_role=getattr(current_user, "role", None),
                    origin="portal_confirm",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        request.form.get("idempotency_key")
                        or f"portal-change:{company.id}:{getattr(current_subscription, 'id', 0)}:{plan.id}:{current_user.id}"
                    ),
                ),
            )
            subscription = Subscription.query.filter_by(
                id=command_result.subscription_id,
                company_id=company.id,
            ).first()
        if subscription is None:
            raise RuntimeError("No se pudo recuperar la suscripción creada por el comando.")

        if float(plan.price or 0) > 0:
            # Keep the old subscription operational while the new paid plan is pending.
            db.session.commit()
            payload = BillingService().create_checkout_for_plan(