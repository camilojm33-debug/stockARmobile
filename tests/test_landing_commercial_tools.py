from types import SimpleNamespace
import json

import pytest

from app import _plan_feature_flags


def test_plan_feature_flags_include_wholesale_and_supplier_purchases():
    flags = _plan_feature_flags(SimpleNamespace(
        features_json="compras,wholesale_pricing,promotions,pricing_controller",
        max_users=8,
        max_products=12000,
        max_clients=12000,
    ))

    assert flags["compras"] is True
    assert flags["mayoristas"] is True
    assert flags["promociones"] is True
    assert flags["controlador_precios"] is True


@pytest.mark.parametrize(
    ("path", "expected_heading", "canonical"),
    [
        (
            "/precios-mayoristas-para-comercios",
            "Precios mayoristas para vender por volumen sin perder el control",
            "precios-mayoristas-para-comercios",
        ),
        (
            "/sistema-de-promociones-comerciales",
            "Creá promociones claras para impulsar las ventas de tu comercio",
            "sistema-de-promociones-comerciales",
        ),
        (
            "/control-global-de-precios",
            "Actualizá muchos precios de una vez, con controles antes de confirmar",
            "control-global-de-precios",
        ),
    ],
)
def test_commercial_tool_seo_pages_are_public_and_have_unique_metadata(app, path, expected_heading, canonical):
    response = app.test_client().get(path)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert expected_heading in body
    assert f"/{canonical}" in body
    assert 'name="description"' in body
    schema_start = body.index('<script type="application/ld+json">') + len('<script type="application/ld+json">')
    schema_end = body.index("</script>", schema_start)
    json.loads(body[schema_start:schema_end])
    assert "Comenzar prueba gratuita" in body


def test_landing_promotes_commercial_tools_and_plan_comparison(app):
    response = app.test_client().get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "herramientas-comerciales" in body
    assert "Precios mayoristas" in body
    assert "Controlador de promociones" in body
    assert "Control global de precios" in body
    assert "Compras a proveedores" in body
    assert "/precios-mayoristas-para-comercios" in body
