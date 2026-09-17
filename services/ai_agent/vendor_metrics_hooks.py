"""Automatic registration for public Vendor IA attribution hooks."""
from flask.signals import appcontext_pushed

from services.ai_agent.vendor_metrics_service import install_metrics_hooks


@appcontext_pushed.connect
def _install_vendor_metrics_hooks(sender, **extra):
    try:
        install_metrics_hooks(sender)
    except Exception:
        sender.logger.exception("No se pudieron registrar las métricas del Vendedor IA.")
