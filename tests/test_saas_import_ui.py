from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_superadmin_prospect_import_forms_include_csrf():
    template = (ROOT / "templates/saas/crm_import.html").read_text(encoding="utf-8")
    assert template.count('name="csrf_token"') >= 2
    assert 'name="action" value="preview"' in template
    assert 'name="action" value="import"' in template


def test_superadmin_manual_consent_controls_are_explicit_and_audited():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates/saas/crm.html").read_text(encoding="utf-8")
    source = (root / "saas.py").read_text(encoding="utf-8")

    assert 'crm_leads_bulk_contact_preferences' in source
    assert 'saas_lead_manual_consent_bulk' in source
    assert 'superadmin_manual' in source
    assert 'name="consent_reason"' in template
    assert 'Autorizar Email' in template
    assert 'Autorizar WhatsApp' in template
    assert 'Autorizar Email + WhatsApp' in template
    assert 'Quitar permisos / bloquear' in template
    assert 'email_status != "invalid"' in source
    assert 'name="step_up_password"' in template


def test_superadmin_crm_bulk_delete_is_available_and_reuses_lead_selection():
    template = (ROOT / "templates/saas/crm.html").read_text(encoding="utf-8")
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    assert 'action="{{ url_for(\'saas.crm_leads_bulk_delete\') }}"' in template
    assert 'id="crmBulkDeleteConfirmForm"' in template
    assert 'name="lead_ids" value="{{ lead.id }}" form="crmBulkDeleteConfirmForm"' in template
    assert "crmBulkDeleteButton" in template
    assert 'def crm_leads_bulk_delete()' in source
    assert 'ids = ids[:100]' in source
    assert 'No se realizaron cambios.' in source
