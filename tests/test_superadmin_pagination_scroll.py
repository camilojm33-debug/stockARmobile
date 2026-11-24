from pathlib import Path


def test_superadmin_pagination_restores_scroll_only_for_same_listing():
    template = Path("templates/base_master.html").read_text(encoding="utf-8")

    assert "current_user.role == 'superadmin'" in template
    assert "stockarmobile:superadmin-pagination-scroll" in template
    assert "listingContext(target.href) !== listingContext(current.href)" in template
    assert "window.scrollY" in template
    assert "window.scrollTo(0, Number(saved.scrollY) || 0)" in template
    assert "anchor.classList.contains('page-link') || anchor.closest('.pagination')" in template
    assert "saved.context !== context || saved.page !== page" in template
    assert "sessionStorage.removeItem(storageKey)" in template
