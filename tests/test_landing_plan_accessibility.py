from pathlib import Path


def test_public_plan_comparison_indicators_are_accessible():
    template = Path("templates/landing/index.html").read_text(encoding="utf-8")
    start = template.index('<section class="section" id="comparador">')
    end = template.index("</section>", start)
    comparison = template[start:end]

    assert '<span class="visually-hidden">Incluido</span>' in comparison
    assert '<span class="visually-hidden">No incluido</span>' in comparison
    assert 'aria-hidden="true" class="bi bi-check2 text-success"' in comparison
    assert 'aria-hidden="true" class="bi bi-dash"' in comparison
    assert '<i class="bi bi-check2 text-success"></i>' not in comparison
    assert '<i class="bi bi-dash"></i>' not in comparison
