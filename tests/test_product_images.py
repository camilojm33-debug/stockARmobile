import io

import pytest

from PIL import Image
from werkzeug.datastructures import FileStorage

from services.product_image_service import ProductImageError, delete_product_image, save_product_image

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import Company, Product, User, db


def _product_edit_fixture(app, tmp_path):
    app.config["PRODUCT_UPLOAD_DIR"] = str(tmp_path)
    with app.app_context():
        company = Company(name="Empresa edición imagen", active=True)
        user = User(
            username="product_image_admin",
            email="product-image-admin@example.test",
            password_hash="test-only",
            role="admin",
            active=True,
            company_id=None,
        )
        db.session.add(company)
        db.session.flush()
        user.company_id = company.id
        product = Product(
            company_id=company.id,
            barcode="IMG-EDIT-001",
            name="Producto original",
            category="Pruebas",
            stock=5,
            min_stock=1,
            cost_price=10,
            price=25,
            active=True,
        )
        old_photo = save_product_image(_image_upload("original.png"))
        product.photo = old_photo
        db.session.add_all([user, product])
        db.session.commit()
        company_id, user_id, product_id = company.id, user.id, product.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True
    old_file = tmp_path / old_photo.rsplit("/", 1)[-1]
    return client, company_id, product_id, old_photo, old_file


def _edit_product_data(*, name="Producto actualizado", photo_file=None):
    data = {
        "barcode": "IMG-EDIT-001",
        "name": name,
        "description": "Descripción actualizada",
        "category": "Pruebas",
        "sale_type": "unidad",
        "unit_measure": "u",
        "brand": "",
        "supplier": "",
        "cost_price": "10",
        "price": "25",
        "tax": "0",
        "stock": "5",
        "min_stock": "1",
        "discount": "0",
    }
    if photo_file is not None:
        data["photo_file"] = photo_file
    return data


def _new_http_image(filename="nueva.png", fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 80, 160)).save(buffer, format=fmt)
    buffer.seek(0)
    return (buffer, filename)


def test_product_edit_replaces_image_only_after_database_commit(app, tmp_path):
    client, company_id, product_id, old_photo, old_file = _product_edit_fixture(app, tmp_path)

    response = client.post(
        f"/productos/edit/{product_id}",
        data=_edit_product_data(photo_file=_new_http_image()),
        follow_redirects=False,
    )

    assert response.status_code in (301, 302)
    with app.app_context():
        updated = Product.query.filter_by(id=product_id, company_id=company_id).one()
        assert updated.name == "Producto actualizado"
        assert updated.photo != old_photo
        new_photo = updated.photo
    new_file = tmp_path / new_photo.rsplit("/", 1)[-1]
    assert new_file.is_file()
    assert not old_file.exists()


def test_product_edit_invalid_image_keeps_previous_photo_and_rolls_back_form(app, tmp_path):
    client, company_id, product_id, old_photo, old_file = _product_edit_fixture(app, tmp_path)

    response = client.post(
        f"/productos/edit/{product_id}",
        data=_edit_product_data(
            name="No debe persistir",
            photo_file=(io.BytesIO(b"esto-no-es-una-imagen"), "invalida.png"),
        ),
        follow_redirects=False,
    )

    assert response.status_code in (301, 302)
    with app.app_context():
        current = Product.query.filter_by(id=product_id, company_id=company_id).one()
        assert current.name == "Producto original"
        assert current.photo == old_photo
    assert old_file.is_file()
    assert len(list(tmp_path.iterdir())) == 1


def test_product_edit_integrity_error_cleans_new_file_and_preserves_old_photo(app, tmp_path, monkeypatch):
    client, company_id, product_id, old_photo, old_file = _product_edit_fixture(app, tmp_path)

    def fail_commit(_session):
        raise IntegrityError("UPDATE products", {}, RuntimeError("forced duplicate"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = client.post(
        f"/productos/edit/{product_id}",
        data=_edit_product_data(photo_file=_new_http_image()),
        follow_redirects=False,
    )

    assert response.status_code in (301, 302)
    with app.app_context():
        current = Product.query.filter_by(id=product_id, company_id=company_id).one()
        assert current.photo == old_photo
        assert current.name == "Producto original"
    assert old_file.is_file()
    assert len(list(tmp_path.iterdir())) == 1


def test_product_edit_unexpected_commit_error_cleans_new_file_and_propagates(app, tmp_path, monkeypatch):
    client, company_id, product_id, old_photo, old_file = _product_edit_fixture(app, tmp_path)

    def fail_commit(_session):
        raise RuntimeError("unexpected database failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="unexpected database failure"):
        client.post(
            f"/productos/edit/{product_id}",
            data=_edit_product_data(photo_file=_new_http_image()),
            follow_redirects=False,
        )

    with app.app_context():
        current = Product.query.filter_by(id=product_id, company_id=company_id).one()
        assert current.photo == old_photo
        assert current.name == "Producto original"
    assert old_file.is_file()
    assert len(list(tmp_path.iterdir())) == 1




def _image_upload(filename="producto.png", fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), (40, 120, 80)).save(buffer, format=fmt)
    buffer.seek(0)
    return FileStorage(stream=buffer, filename=filename)


def test_product_image_service_validates_real_image_and_persists(tmp_path, app):
    app.config["PRODUCT_UPLOAD_DIR"] = str(tmp_path)

    with app.app_context():
        photo = save_product_image(_image_upload())

        assert photo.startswith("/productos/imagen/")
        filename = photo.rsplit("/", 1)[-1]
        stored = tmp_path / filename
        assert stored.is_file()

        response = app.test_client().get(photo)
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        assert response.data
        response.close()

        delete_product_image(photo)
        assert not stored.exists()


def test_product_image_service_rejects_fake_image_with_allowed_extension(tmp_path, app):
    app.config["PRODUCT_UPLOAD_DIR"] = str(tmp_path)

    fake = FileStorage(stream=io.BytesIO(b"not-an-image"), filename="producto.png")

    with app.app_context():
        try:
            save_product_image(fake)
        except ProductImageError as exc:
            assert "imagen válida" in str(exc)
        else:
            raise AssertionError("Se aceptó un archivo que no era una imagen")


def test_product_image_service_rejects_images_over_5mb(tmp_path, app):
    app.config["PRODUCT_UPLOAD_DIR"] = str(tmp_path)
    upload = FileStorage(stream=io.BytesIO(b"x" * (5 * 1024 * 1024 + 1)), filename="producto.png")

    with app.app_context():
        try:
            save_product_image(upload)
        except ProductImageError as exc:
            assert "5 MB" in str(exc)
        else:
            raise AssertionError("Se aceptó una imagen mayor a 5 MB")


def test_product_image_service_replacement_removes_previous_file(tmp_path, app):
    app.config["PRODUCT_UPLOAD_DIR"] = str(tmp_path)

    with app.app_context():
        first = save_product_image(_image_upload("first.jpg", "JPEG"))
        second = save_product_image(_image_upload("second.webp", "WEBP"))

        first_path = tmp_path / first.rsplit("/", 1)[-1]
        second_path = tmp_path / second.rsplit("/", 1)[-1]
        assert first_path.exists()
        assert second_path.exists()

        delete_product_image(first)
        assert not first_path.exists()
        assert second_path.exists()
