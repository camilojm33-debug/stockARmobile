from pathlib import Path

def test_operational_cli_commands_are_registered_in_app_source():
    source = Path("app.py").read_text(encoding="utf-8")
    assert '@app.cli.command("migrate-legacy-product-images")' in source
    assert '@app.cli.command("backup-company")' in source
    assert '@click.option("--company-id", type=int, required=True)' in source


def test_legacy_image_migration_is_scoped_to_old_public_path():
    source = Path("app.py").read_text(encoding="utf-8")
    assert 'Product.photo.like("/static/uploads/products/%")' in source
    assert 'product.photo = f"/productos/imagen/{filename}"' in source
