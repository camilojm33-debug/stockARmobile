from pathlib import Path


def test_company_delete_is_outside_fk_loop():
    text = Path("saas.py").read_text(encoding="utf-8")
    marker = "for table_name in table_names:"
    start = text.index(marker)
    end = text.index("def _action_allowed_for_status", start)
    block = text[start:end]
    assert block.count("DELETE FROM companies WHERE id = :company_id") == 0
    assert "DELETE FROM companies WHERE id = :company_id" in text[end - 500:end + 500]
