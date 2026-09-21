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
