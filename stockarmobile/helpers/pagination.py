"""Pagination helper utilities."""

from .numbers import safe_int


def resolve_pagination(page_value, per_page_value, *, default_page=1, default_per_page=20, max_per_page=100):
    page = max(default_page, safe_int(page_value, default_page))
    per_page = safe_int(per_page_value, default_per_page)
    per_page = max(1, min(per_page, max_per_page))
    return page, per_page
