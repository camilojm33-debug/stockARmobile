import io

from PIL import Image
from werkzeug.datastructures import FileStorage

from services.product_image_service import ProductImageError, delete_product_image, save_product_image


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
