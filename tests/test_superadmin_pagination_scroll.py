from pathlib import Path


def test_superadmin_pagination_restores_scroll_only_for_same_listing():
    template = Path("templates/base_master.html").read_text(encoding="utf-8")

    assert "current_user.role == 'superadmin'" in template
    assert "stockarmobile:superadmin-pagination-scroll" in template
    assert "listingContext(target.href) !== listingContext(current.href)" in template
    assert "window.scrollY" in template
    assert "window.scrollTo(0, Number(saved.scrollY) || 0)" in template
    # Pagination is detected by its page query parameter, not by Bootstrap-only markup.
    assert "!target.searchParams.has('page')" in template
    assert "anchor.closest('.pagination')" not in template
    assert "saved.context !== context || saved.page !== page" in template
    assert "sessionStorage.removeItem(storageKey)" in template


def test_superadmin_crm_prospect_pager_uses_generic_pagination_scroll_handler():
    base_template = Path("templates/base_master.html").read_text(encoding="utf-8")
    crm_template = Path("templates/saas/crm.html").read_text(encoding="utf-8")

    # The CRM prospect pager uses plain button anchors inside a flex div,
    # not Bootstrap .pagination / .page-link classes.
    assert "page=lead_page-1" in crm_template
    assert "page=lead_page+1" in crm_template
    assert "!target.searchParams.has('page')" in base_template
    assert "listingContext(target.href) !== listingContext(current.href)" in base_template
