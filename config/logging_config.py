"""Logging profesional para despliegues locales y Render."""

import logging
from logging.config import dictConfig


def configure_logging():
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "level": "INFO",
                }
            },
            "root": {"level": "INFO", "handlers": ["console"]},
        }
    )
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
