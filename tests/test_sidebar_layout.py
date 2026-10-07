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
    assert "flex: 1 1 auto;" in nav_css
    assert "min-height: 0;" in nav_css
    assert "height: 100%;" in nav_css
    assert "overflow-y: auto;" in nav_css
    assert "overscroll-behavior: contain;" in nav_css
