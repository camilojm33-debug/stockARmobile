"""Regression tests for SuperAdmin manual AI subscription renewal."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_expired_manual_ai_subscription_has_one_prominent_renew_action():
    template = (ROOT / "templates/saas/ai_subscription_detail.html").read_text(encoding="utf-8")

    assert "Renovar suscripción IA vencida" in template
    assert "Renovar y restablecer acceso" in template
    assert "El plan <strong>{{ status.plan_name or status.plan_code }}</strong> sigue asignado, pero su vigencia terminó." in template
    assert "asignar o cambiar el plan aquí no renueva la vigencia" in template
    assert "{% if status.status != 'VENCIDA' %}" in template
    assert "La renovación manual está disponible en la tarjeta destacada de arriba." in template


def test_superadmin_renew_action_reports_result_and_expiry_date():
    source = (ROOT / "saas.py").read_text(encoding="utf-8")
    start = source.index("def ai_subscriptions_action(company_id):")
    end = source.index('@bp.route("/companies")', start)
    route = source[start:end]

    assert 'elif action == "renew":' in route
    assert 'result = AISubscriptionService.renew(company' in route
    assert 'if action == "renew":' in route
    assert "Nuevo vencimiento: {new_expiry}" in route
    assert "final_status == \"VENCIDA\"" in route
    assert "Asignar un plan no renueva la vigencia" in route
