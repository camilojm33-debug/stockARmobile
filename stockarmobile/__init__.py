"""StockArmobile application factory."""

from flask import Flask

from .config import configure_app
from .extensions import init_extensions


def create_app(import_name=__name__, **flask_kwargs):
    """Create and configure the Flask application instance."""
    app = Flask(import_name, **flask_kwargs)
    configure_app(app)
    init_extensions(app)
    return app
