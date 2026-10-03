from pathlib import Path


def test_superadmin_dashboard_exposes_commercial_data_quality():
    source = Path("saas.py").read_text(encoding="utf-8")
    template = Path("templates/saas/index.html").read_text(encoding="utf-8")

    assert "commercial_data_quality" in source
    assert 'Payment.status == "approved"' in source
    assert 'Payment.status.in_(["pending", "authorized", "in_process"])' in source
    assert 'Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])' in source
    assert "Subscription.next_billing_date > now + timedelta(days=366)" in source
    assert "Subscription.ends_at < Subscription.starts_at" in source
    assert "Control comercial y calidad de datos" in template
    assert "No contar como cobrado" in template
