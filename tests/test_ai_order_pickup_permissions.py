import json


def _login_pickup_test_user(test_client, user):
    with test_client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_pedidos_ia_pickup_requires_ai_access_for_employees(app):
    """The pickup mutation must follow the same employee permission as Pedidos IA."""
    from app import Company, User, db

    company = Company(name="Empresa permisos Pedidos IA", active=True)
    db.session.add(company)
    db.session.flush()

    employee_without_ai = User(
        username="pickup_employee_without_ai",
        email="pickup-without-ai@test.local",
        password_hash="not-used",
        role="user",
        active=True,
        company_id=company.id,
        permissions_json="[]",
    )
    employee_with_ai = User(
        username="pickup_employee_with_ai",
        email="pickup-with-ai@test.local",
        password_hash="not-used",
        role="user",
        active=True,
        company_id=company.id,
        permissions_json=json.dumps(["ai_access"]),
    )
    admin = User(
        username="pickup_admin",
        email="pickup-admin@test.local",
        password_hash="not-used",
        role="admin",
        active=True,
        company_id=company.id,
        permissions_json="[]",
    )
    db.session.add_all([employee_without_ai, employee_with_ai, admin])
    db.session.commit()

    def post_as(user):
        test_client = app.test_client()
        _login_pickup_test_user(test_client, user)
        return test_client.post("/pedidos-ia/999999/retirar")

    # Permission denial happens in the global endpoint guard before the view
    # can look up or mutate a quote.
    assert post_as(employee_without_ai).status_code == 403

    # A permitted employee and an admin both pass the permission guard; the
    # missing test quote then yields 404 from the route itself.
    assert post_as(employee_with_ai).status_code == 404
    assert post_as(admin).status_code == 404
