from pathlib import Path


def test_render_blueprint_uses_existing_production_database_and_readiness_probe():
    render_config = (Path(__file__).resolve().parents[1] / "render.yaml").read_text(encoding="utf-8")

    assert "healthCheckPath: /readyz" in render_config
    assert "name: stockarmobile-prod" in render_config
    assert "databaseName: stockarmobile_prod" in render_config
    assert "ipAllowList: []" in render_config
    assert "name: stockarmobile-db" not in render_config
