from pathlib import Path
import re


def test_main_sidebar_has_fixed_header_scroll_navigation_and_footer():
    template = Path("templates/base_master.html").read_text(encoding="utf-8")
    sidebar = template.split('<aside class="app-sidebar" id="appSidebar">', 1)[1].split("</aside>", 1)[0]

    header_start = sidebar.index('<div class="app-sidebar-header">')
    brand_start = sidebar.index('<a class="app-brand"', header_start)
    scroll_start = sidebar.index('<div class="app-sidebar-nav-scroll">', brand_start)
    nav_start = sidebar.index('<nav class="app-nav nav flex-column">', scroll_start)
    nav_end = sidebar.index("</nav>", nav_start) + len("</nav>")
    scroll_end = sidebar.index("</div>", nav_end)
    footer_start = sidebar.index('<div class="app-sidebar-footer" id="appSidebarFooter">', scroll_end)

    assert header_start < brand_start < scroll_start < nav_start < scroll_end < footer_start
    assert nav_end < scroll_end
    assert "app-sidebar-footer" not in sidebar[scroll_start:scroll_end]

    nav_markup = sidebar[nav_start:scroll_end]
    assert len(re.findall(r'class="nav-link', nav_markup)) == 54
    assert re.findall(r'class="nav-section-label">([^<]+)</div>', nav_markup) == [
        "Administración",
        "IA",
        "Operación",
        "Referidos",
        "Operación",
        "Comercial",
        "Resumen",
    ]


def test_sidebar_navigation_wrapper_is_the_scroll_owner():
    stylesheet = Path("static/assets/css/styles.css").read_text(encoding="utf-8")

    def rule(selector):
        match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", stylesheet)
        assert match, f"Missing CSS rule: {selector}"
        return match.group(1)

    sidebar_css = rule(".app-sidebar")
    scroll_css = rule(".app-sidebar-nav-scroll")
    nav_css = rule(".app-nav")
    footer_css = rule(".app-sidebar-footer")

    assert "position: fixed;" in sidebar_css
    assert "height: 100dvh;" in sidebar_css
    assert "overflow: hidden;" in sidebar_css
    assert "overflow-y: auto;" in scroll_css
    assert "min-height: 0;" in scroll_css
    assert "-webkit-overflow-scrolling: touch;" in scroll_css
    assert "overscroll-behavior: contain;" in scroll_css
    assert "overflow-y" not in sidebar_css
    assert "overflow-y" not in nav_css
    assert "flex: 0 0 auto;" in footer_css
