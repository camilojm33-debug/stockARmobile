from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_superadmin_prospect_import_forms_include_csrf():
    template = (ROOT / "templates/saas/crm_import.html").read_text(encoding="utf-8")
    assert template.count('name="csrf_token"') >= 2
    assert 'name="action" value="preview"' in template
    assert 'name="action" value="import"' in template
