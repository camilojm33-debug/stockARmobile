"""StockArmobile application factory."""

from flask import Flask

from .config import configure_app
from .extensions import init_extensions


def create_app(import_name=__name__, **flask_kwargs):
    """Create and configure the Flask application instance."""
    app = Flask(import_name, **flask_kwargs)
    configure_app(app)
    init_extensions(app)
    import stockarmobile.models  # noqa: E402,F401

    # The legacy app imports notification helpers directly. Hook the existing
    # notification builder instead of duplicating or replacing the center.
    try:
        import services.notification_service as notification_service
        from services.ai_agent.order_notifications import build_ai_order_notifications

        if not getattr(notification_service, "_ai_orders_notification_hook", False):
            original_builder = notification_service.build_notifications

            def build_notifications_with_ai_orders():
                items = list(original_builder() or [])
                try:
                    ai_items = build_ai_order_notifications()
                except Exception:
                    app.logger.exception("AI order notification hook failed")
                    ai_items = []
                return ai_items + items

            notification_service.build_notifications = build_notifications_with_ai_orders
            notification_service._ai_orders_notification_hook = True
    except Exception:
        app.logger.exception("Could not install AI order notification hook")

    return app
