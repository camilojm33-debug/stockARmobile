from types import SimpleNamespace


def test_pedidos_ia_pickup_requires_ai_access_for_employees():
    """The pickup route must be mapped to the same permission as Pedidos IA."""
    from app import _user_has_permission
    from stockarmobile.permissions import employee_endpoint_permission

    endpoint = "whatsapp_agent.ai_order_mark_picked_up"
    required = employee_endpoint_permission(endpoint, "POST")

    assert required == "ai_access"

    employee_without_ai = SimpleNamespace(role="user", permissions_json="[]")
    employee_with_ai = SimpleNamespace(role="user", permissions_json='["ai_access"]')
    admin = SimpleNamespace(role="admin", permissions_json="[]")

    # These are the exact permission checks used by the global before_request guard.
    assert _user_has_permission(employee_without_ai, required) is False
    assert _user_has_permission(employee_with_ai, required) is True
    # Administrators bypass the employee permission matrix.
    assert _user_has_permission(admin, required) is True
