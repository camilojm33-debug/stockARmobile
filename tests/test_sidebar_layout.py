from pathlib import Path


def test_main_sidebar_owns_scroll_in_navigation_area():
    template = Path("templates/base_master.html").read_text(encoding="utf-8")

    sidebar_start = template.index("    .app-sidebar {")
    nav_start = template.index("    .app-nav {", sidebar_start)
    main_start = template.index("    .app-main {", nav_start)

    sidebar_css = template[sidebar_start:nav_start]
    nav_css = template[nav_start:main_start]

    assert "display: grid;" in sidebar_css
    assert "grid-template-rows: auto minmax(0, 1fr) auto;" in sidebar_css
    assert "overflow: hidden;" in sidebar_css
    assert "min-height: 0;" in nav_css
    assert "height: 100%;" in nav_css
    assert "overflow-y: auto;" in nav_css
    assert "overscroll-behavior: contain;" in nav_css


def test_sidebar_final_stylesheet_owns_scroll_behavior():
    template = Path("static/assets/css/styles.css").read_text(encoding="utf-8")
    marker = "/* Sidebar scroll definitivo: viewport acotado igual que un panel flotante. */"
    assert marker in template
    final_css = template.split(marker, 1)[1]

    assert ".app-sidebar > .app-nav" in final_css
    assert "position: absolute !important;" in final_css
    assert "top: 64px !important;" in final_css
    assert "bottom: 76px !important;" in final_css
    assert "overflow-y: scroll !important;" in final_css
    assert "touch-action: pan-y !important;" in final_css
    assert ".app-sidebar > .app-sidebar-footer" in final_css
    assert "position: absolute !important;" in final_css
    assert "bottom: 0 !important;" in final_css
    assert "@media (max-width: 1099.98px)" in final_css
