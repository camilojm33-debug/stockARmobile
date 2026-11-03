from pathlib import Path


def test_pos_inline_javascript_uses_api_branch_stock_without_python_syntax():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "ventas" / "index.html").read_text(encoding="utf-8")

    invalid_js_expression = (
        "Number((product.branch_stock_display if "
        "product.branch_stock_display is defined else product.stock) || 0)"
    )

    # The POS search and barcode endpoints already return the active-branch
    # availability in the `stock` field. A Python/Jinja conditional inside
    # this JavaScript block prevents the entire inline POS script from parsing,
    # leaving every card's "Agregar" handler undefined.
    assert invalid_js_expression not in template
    assert template.count("Number(product.stock || 0)") == 3
    assert "function handleAddToCartClick(button)" in template
    assert "window.handleAddToCartClick = handleAddToCartClick;" in template
