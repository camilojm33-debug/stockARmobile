from app import Branch, Company, User, UserBranchAccess, db


def _company(name):
    company = Company(name=name, active=True, business_pin_hash="test-business-pin-hash")
    db.session.add(company)
    db.session.flush()
    return company


def _branch(company, name, code):
    branch = Branch(
        company_id=company.id,
        name=name,
        code=code,
        active=True,
        is_default=False,
    )
    db.session.add(branch)
    db.session.flush()
    return branch


def _user(company, username, *, role="user", active=True):
    user = User(
        username=username,
        email=f"{username}@example.test",
        password_hash="test-hash",
        role=role,
        active=active,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _route_urls(app, branch_id):
    from flask import url_for

    with app.test_request_context("/"):
        return (
            url_for("branches.assign_users", branch_id=branch_id),
            url_for("company_billing.company_settings_pin_verify"),
        )


def _allow_branch_feature(monkeypatch):
    import branches

    monkeypatch.setattr(branches, "_require_feature", lambda _company: None)


def _allow_pin_verification(monkeypatch, *, valid):
    import company_billing

    monkeypatch.setattr(
        company_billing.CompanySecurityService,
        "remaining_block_seconds",
        staticmethod(lambda _company, now=None: 0),
    )
    monkeypatch.setattr(
        company_billing.CompanySecurityService,
        "verify_pin",
        staticmethod(lambda _company, _pin: valid),
    )
    monkeypatch.setattr(
        company_billing.CompanySecurityService,
        "reset_attempts",
        staticmethod(lambda _company: None),
    )


def test_assign_users_persists_selected_active_employees_for_that_branch(app, monkeypatch):
    import company_billing

    _allow_branch_feature(monkeypatch)
    monkeypatch.setattr(company_billing, "_is_pin_verified", lambda _company_id: True)

    with app.app_context():
        company = _company("Empresa asignación de personal")
        branch = _branch(company, "25 de Mayo", "25M")
        old_employee = _user(company, "employee-old")
        selected_employee = _user(company, "employee-selected")
        admin = _user(company, "branch-assignment-admin", role="admin")
        db.session.add(UserBranchAccess(
            company_id=company.id,
            branch_id=branch.id,
            user_id=old_employee.id,
        ))
        db.session.commit()

        client = app.test_client()
        _login(client, admin)
        assign_url, _pin_url = _route_urls(app, branch.id)
        response = client.post(assign_url, data={"user_ids": [str(selected_employee.id)]})

        assert response.status_code == 302
        assert response.headers["Location"].endswith("panel=branches")
        saved_ids = {
            row.user_id for row in UserBranchAccess.query.filter_by(
                company_id=company.id, branch_id=branch.id
            ).all()
        }
        assert saved_ids == {selected_employee.id}


def test_assign_users_replays_pending_selection_after_successful_pin(app, monkeypatch):
    _allow_branch_feature(monkeypatch)
    _allow_pin_verification(monkeypatch, valid=True)

    with app.app_context():
        company = _company("Empresa asignación pendiente")
        branch = _branch(company, "Sucursal Norte", "NORTE")
        selected_employee = _user(company, "employee-pending")
        admin = _user(company, "branch-pending-admin", role="admin")
        db.session.commit()

        client = app.test_client()
        _login(client, admin)
        assign_url, pin_url = _route_urls(app, branch.id)

        # Submitting access changes before PIN verification must stage, not lose,
        # the exact selection and must not claim it has already been saved.
        response = client.post(assign_url, data={"user_ids": [str(selected_employee.id)]})
        assert response.status_code == 302
        assert response.headers["Location"].endswith("panel=branches")
        assert UserBranchAccess.query.filter_by(
            company_id=company.id, branch_id=branch.id, user_id=selected_employee.id
        ).first() is None

        from branches import PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY
        with client.session_transaction() as session:
            pending = session.get(PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY)
            assert pending["branch_id"] == branch.id
            assert pending["user_ids"] == [selected_employee.id]

        response = client.post(pin_url, data={"access_pin": "1234"})

        assert response.status_code == 302
        assert response.headers["Location"].endswith("panel=branches")
        assert UserBranchAccess.query.filter_by(
            company_id=company.id, branch_id=branch.id, user_id=selected_employee.id
        ).first() is not None
        with client.session_transaction() as session:
            assert PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY not in session


def test_invalid_pin_keeps_pending_branch_assignment_without_granting_access(app, monkeypatch):
    _allow_branch_feature(monkeypatch)
    _allow_pin_verification(monkeypatch, valid=False)

    with app.app_context():
        company = _company("Empresa PIN incorrecto")
        branch = _branch(company, "Sucursal Centro", "CENTRO")
        selected_employee = _user(company, "employee-pin-pending")
        admin = _user(company, "branch-pin-admin", role="admin")
        db.session.commit()

        client = app.test_client()
        _login(client, admin)
        assign_url, pin_url = _route_urls(app, branch.id)
        client.post(assign_url, data={"user_ids": [str(selected_employee.id)]})

        response = client.post(pin_url, data={"access_pin": "0000"})

        assert response.status_code == 302
        assert response.headers["Location"].endswith("panel=branches")
        assert UserBranchAccess.query.filter_by(
            company_id=company.id, branch_id=branch.id, user_id=selected_employee.id
        ).first() is None

        from branches import PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY
        with client.session_transaction() as session:
            pending = session.get(PENDING_BRANCH_USER_ASSIGNMENT_SESSION_KEY)
            assert pending["branch_id"] == branch.id
            assert pending["user_ids"] == [selected_employee.id]


def test_branch_employee_assignment_rejects_inactive_and_foreign_employees(app, monkeypatch):
    import company_billing

    _allow_branch_feature(monkeypatch)
    monkeypatch.setattr(company_billing, "_is_pin_verified", lambda _company_id: True)

    with app.app_context():
        company = _company("Empresa empleados asignables")
        other_company = _company("Otra empresa empleados")
        branch = _branch(company, "Sucursal", "SUC")
        inactive_employee = _user(company, "employee-inactive", active=False)
        foreign_employee = _user(other_company, "employee-foreign")
        admin = _user(company, "branch-validation-admin", role="admin")
        db.session.commit()

        client = app.test_client()
        _login(client, admin)
        assign_url, _pin_url = _route_urls(app, branch.id)
        response = client.post(assign_url, data={
            "user_ids": [str(inactive_employee.id), str(foreign_employee.id)]
        })

        assert response.status_code == 302
        assert UserBranchAccess.query.filter_by(
            company_id=company.id, branch_id=branch.id
        ).count() == 0


def test_branch_employee_assignment_form_distinguishes_branch_details_from_access_save(app):
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "company_billing"
        / "settings.html"
    ).read_text(encoding="utf-8")

    assert "Guardar datos de sucursal" in template
    assert "Guardar asignación de personal" in template
    assert "Esta acción no modifica los datos generales de la sucursal." in template
    assert "asignación de personal pendiente de confirmación" in template.lower()
