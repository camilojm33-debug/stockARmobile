import re


def test_onboarding_progress_starts_from_real_tenant_setup(app):
    from app import Company, User, db

    with app.app_context():
        company = Company(
            name="Empresa Onboarding QA",
            active=True,
            currency="ARS",
            timezone="America/Argentina/Buenos_Aires",
            date_format="%Y-%m-%d",
            numbering_format="es_AR",
        )
        db.session.add(company)
        db.session.flush()
        user = User(
            username="onboarding_qa_admin",
            email="onboarding-qa@example.test",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("onboarding-password")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    login_response = client.post(
        "/auth/login",
        data={"username": "onboarding_qa_admin", "password": "onboarding-password"},
    )
    assert login_response.status_code in (301, 302)

    response = client.get("/dashboard/onboarding")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    progress_match = re.search(r"\d+ de 6 pasos completados · (\d+)%", html)
    assert progress_match is not None
    assert int(progress_match.group(1)) < 100
    assert "Cargar productos" in html
    assert "Agregar clientes" in html
    assert "Registrar venta" in html
    assert "Pendiente" in html
