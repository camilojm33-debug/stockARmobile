def test_consent_model_and_public_workflow_contract():
    from app import SaaSLeadConsent
    from saas import crm_consent

    assert hasattr(SaaSLeadConsent, "consent_token")
    source = __import__("inspect").getsource(crm_consent)
    assert 'email_opted_in' in source
    assert 'whatsapp_opted_in' in source
    assert 'phone_opted_in' in source
    assert 'reject_all' in source
    assert 'public_consent_link' in source


def test_consent_migration_is_separate_from_unsubscribe_token():
    from pathlib import Path
    migration = Path("migrations/versions/20261003_01_saas_consent_tokens.py").read_text(encoding="utf-8")
    assert "consent_token" in migration
    assert "unsubscribe_token" not in migration


def test_consent_page_is_channel_specific():
    from pathlib import Path
    template = Path("templates/saas/crm_consent.html").read_text(encoding="utf-8")
    assert 'name="email_opted_in"' in template
    assert 'name="whatsapp_opted_in"' in template
    assert 'name="phone_opted_in"' in template
    assert 'name="reject_all"' in template
