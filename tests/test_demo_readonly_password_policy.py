"""Regression tests for the shared demo and password policy."""

from app import RegisterForm, User, db
from services.password_recovery_service import PasswordRecoveryService


def test_shared_demo_is_read_only_but_can_logout(app):
    client = app.test_client()

    response = client.get("/auth/demo", follow_redirects=False)
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("demo_mode") is True

    blocked = client.post("/auth/switch-user", data={"target_user_id": "1"})
    assert blocked.status_code == 403
    assert b"solo de lectura" in blocked.get_data(as_text=True).lower()

    logout = client.post("/auth/logout", follow_redirects=False)
    assert logout.status_code == 302
    with client.session_transaction() as sess:
        assert not sess.get("demo_mode")


def test_registration_form_requires_eight_character_passwords(app):
    with app.test_request_context("/auth/register", method="POST"):
        form = RegisterForm(
            data={
                "username": "Empresa QA",
                "email": "password-policy@example.test",
                "password": "123456",
            }
        )

        assert form.validate() is False
        assert form.password.errors


def test_password_reset_rejects_seven_character_password(app, monkeypatch):
    monkeypatch.setattr(
        PasswordRecoveryService,
        "get_valid_token_row",
        classmethod(lambda cls, *, raw_token: object()),
    )
    response = app.test_client().post(
        "/auth/reset-password/test-token",
        data={"new_password": "1234567", "confirm_password": "1234567"},
    )

    assert response.status_code == 200
    assert "8 caracteres" in response.get_data(as_text=True)


def test_forced_password_change_rejects_seven_character_password(app):
    with app.app_context():
        user = User(
            username="forced_password_qa",
            email="forced-password-qa@example.test",
            role="superadmin",
            active=True,
            must_change_password=True,
        )
        user.set_password("old-password-123")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True

    response = client.post(
        "/auth/force-password-change",
        data={"new_password": "1234567", "confirm_password": "1234567"},
    )
    assert response.status_code == 200
    assert "8 caracteres" in response.get_data(as_text=True)

    with app.app_context():
        persisted_user = db.session.get(User, user_id)
        assert persisted_user is not None
        assert persisted_user.must_change_password is True
