from pathlib import Path


def test_company_delete_is_outside_fk_loop():
    text = Path("saas.py").read_text(encoding="utf-8")
    loop_marker = "for table_name in table_names:"
    loop_start = text.index(loop_marker)
    delete_section_marker = "    # Delete the tenant only after all known dependent rows and FK-referenced"
    delete_section_start = text.index(delete_section_marker, loop_start)
    loop_region = text[loop_start:delete_section_start]

    # The FK cleanup loop must never execute the tenant DELETE itself.
    assert "DELETE FROM companies WHERE id = :company_id" not in loop_region

    # The tenant DELETE must remain after the dependency cleanup loop.
    delete_statement = "DELETE FROM companies WHERE id = :company_id"
    assert delete_statement in text[delete_section_start:]
