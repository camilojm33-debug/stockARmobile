from pathlib import Path


def test_superadmin_critical_actions_require_step_up():
    text = Path("saas.py").read_text(encoding="utf-8")

    delete_block = text[text.index("def company_delete("):text.index("def company_impersonate(")]
    restore_block = text[text.index("def backups_restore("):text.index("def backups_delete(")]

    assert "_require_superadmin_step_up()" in delete_block
    assert "_require_superadmin_step_up()" in restore_block
    assert "SUPERADMIN_STEP_UP_TTL_SECONDS = 600" in text
    assert "superadmin_step_up_failed" in text
    assert "superadmin_step_up_success" in text


def test_superadmin_critical_action_forms_expose_reauthentication():
    companies = Path("templates/saas/companies.html").read_text(encoding="utf-8")
    detail = Path("templates/saas/company_detail.html").read_text(encoding="utf-8")
    backups = Path("templates/saas/backups.html").read_text(encoding="utf-8")

    assert 'name="step_up_password"' in companies
    assert 'name="step_up_password"' in detail
    assert 'name="step_up_password"' in backups
    assert 'autocomplete="current-password"' in companies
    assert 'autocomplete="current-password"' in backups


def test_superadmin_backup_routes_are_wired_to_the_correct_handlers():
    text = Path("saas.py").read_text(encoding="utf-8")

    verify_marker = '@bp.route("/backups/<int:backup_id>/verify", methods=["POST"])\n@superadmin_required\ndef backups_verify(backup_id):'
    restore_marker = '@bp.route("/backups/<int:backup_id>/restore", methods=["POST"])\n@superadmin_required\ndef backups_restore(backup_id):'

    assert verify_marker in text
    assert restore_marker in text
    assert '@bp.post("/superadmin/backups/<int:backup_id>/verify")' not in text


def test_superadmin_privileged_access_actions_require_step_up():
    text = Path("saas.py").read_text(encoding="utf-8")

    guarded_functions = [
        "toggle_company",
        "company_assign_pin",
        "company_generate_pin",
        "company_impersonate",
        "users_update_role",
        "users_update_status",
        "password_recovery_company_user_reset",
        "password_recovery_reset",
    ]
    for function_name in guarded_functions:
        start = text.index(f"def {function_name}(")
        next_def = text.find("\ndef ", start + 5)
        block = text[start:] if next_def == -1 else text[start:next_def]
        assert "_require_superadmin_step_up()" in block, function_name


def test_superadmin_subscription_mutations_require_step_up():
    text = Path("saas.py").read_text(encoding="utf-8")

    guarded_functions = [
        "ai_subscriptions_action",
        "subscriptions_quick_renew_company",
        "subscriptions_create",
        "subscriptions_update",
        "subscriptions_action",
    ]
    for function_name in guarded_functions:
        start = text.index(f"def {function_name}(")
        next_def = text.find("\ndef ", start + 5)
        block = text[start:] if next_def == -1 else text[start:next_def]
        assert "_require_superadmin_step_up()" in block, function_name

    wsgi = Path("wsgi.py").read_text(encoding="utf-8")
    start = wsgi.index("def superadmin_delete_historical_subscription(")
    block = wsgi[start:]
    assert "_require_superadmin_step_up()" in block


def test_superadmin_subscription_forms_expose_reauthentication():
    subscriptions = Path("templates/saas/subscriptions.html").read_text(encoding="utf-8")
    ai_detail = Path("templates/saas/ai_subscription_detail.html").read_text(encoding="utf-8")

    assert 'name="step_up_password"' in subscriptions
    assert 'autocomplete="current-password"' in subscriptions
    assert 'name="step_up_password"' in ai_detail
    assert 'autocomplete="current-password"' in ai_detail


def test_superadmin_referral_network_mutations_require_step_up():
    text = Path("services/referral_network_service.py").read_text(encoding="utf-8")

    guarded_functions = [
        "link_seller",
        "unlink_seller",
        "set_percent",
        "payout_network",
    ]
    for function_name in guarded_functions:
        start = text.index(f"def {function_name}(")
        next_def = text.find("\ndef ", start + 5)
        block = text[start:] if next_def == -1 else text[start:next_def]
        assert "_require_superadmin_step_up()" in block, function_name

    template = Path("templates/saas/referrals_network.html").read_text(encoding="utf-8")
    assert template.count('name="step_up_password"') == 4
    assert template.count('autocomplete="current-password"') == 4
