import pytest

import app as stock_app
from app import db


@pytest.fixture
def app():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        yield stock_app.app
        db.session.remove()
        db.drop_all()
