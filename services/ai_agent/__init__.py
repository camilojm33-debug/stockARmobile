"""AI Agent orchestration package."""

from .orchestrator import AgentOrchestrator
from . import vendor_publication  # Registers stable Vendor IA routes on app context.
from . import vendor_checkout_public  # Registers public checkout/order hardening routes.
from . import vendor_metrics_service  # Registers Vendor IA attribution hooks.

__all__ = ["AgentOrchestrator"]
