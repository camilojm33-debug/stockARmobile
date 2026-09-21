from pathlib import Path

from app import app


def test_operational_cli_commands_are_registered():
    runner = app.test_cli_runner()

    result = runner.invoke(args=["migrate-legacy-product-images"])
    assert result.exit_code == 0
    assert "legacy_product_images" in result.output


def test_backup_company_cli_requires_company_id():
    runner = app.test_cli_runner()

    result = runner.invoke(args=["backup-company"])
    assert result.exit_code != 0
    assert "--company-id" in result.output


def test_legacy_image_migration_is_scoped_to_old_public_path():
    source = Path("app.py").read_text(encoding="utf-8")
    assert 'Product.photo.like("/static/uploads/products/%")' in source
    assert 'product.photo = f"/productos/imagen/{filename}"' in source
