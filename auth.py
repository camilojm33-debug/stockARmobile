"""Blueprint de autenticacion: login, registro y recuperacion de contrasena."""

from datetime import timedelta
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

bp = Blueprint("auth", __name__, template_folder="templates")


def _login_user_and_bind_company(user, remember=False):
    session.clear()
    login_user(user, remember=remember)


def _post_login_redirect():
    return url_for("saas.index") if getattr(current_user, "role", None) == "superadmin" else url_for("dashboard.index")


def _is_safe_redirect(target):
    if not target:
        return False
    parsed = urlsplit(target)
    return not parsed.netloc and parsed.path.startswith("/")


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Login de usuario local."""
    from app import LoginForm, User, db, record_audit

    if request.method == "POST":
        form = LoginForm()
        if "email" in request.form and "username" not in request.form:
            form.username.data = request.form.get("email", "")
        if form.validate_on_submit():
            username_or_email = form.username.data.strip().lower()

            user = User.query.filter(
                (db.func.lower(User.username) == username_or_email)
                | (db.func.lower(User.email) == username_or_email)
            ).first()

            if user and user.active and user.check_password(form.password.data):
                _login_user_and_bind_company(user, remember=form.remember.data)
                record_audit(action="login_success", entity="user", entity_id=user.id, detail="Inicio de sesion exitoso")
                db.session.commit()
                if getattr(user, "must_change_password", False):
                    flash("Debes cambiar tu contrasena para continuar.", "warning")
                    return redirect(url_for("auth.force_password_change"))
                next_page = (request.form.get("next") or request.args.get("next") or "").strip()
                if not _is_safe_redirect(next_page):
                    next_page = None
                flash("Inicio de sesion exitoso", "success")
                return redirect(next_page if next_page else _post_login_redirect())

            record_audit(action="login_failed", entity="user", detail=f"Intento de login fallido: {username_or_email}")
            db.session.commit()
            flash("Usuario o contrasena incorrectos.", "danger")

    form = LoginForm()
    return render_template("auth/login.html", form=form)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    from app import PasswordRecoveryRequest, User, db, record_audit

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email:
            flash("Debes ingresar un correo electronico.", "danger")
            return render_template("auth/forgot_password.html", email="")

        user = User.query.filter(db.func.lower(User.email) == email).first()
        if user is not None:
            existing = (
                PasswordRecoveryRequest.query.filter_by(user_id=user.id)
                .filter(PasswordRecoveryRequest.status.in_(["pendiente", "atendida"]))
                .order_by(PasswordRecoveryRequest.requested_at.desc())
                .first()
            )
            if existing is None:
                req = PasswordRecoveryRequest(
                    user_id=user.id,
                    company_id=user.company_id,
                    email=user.email,
                    status="pendiente",
                )
                db.session.add(req)
                db.session.flush()
                record_audit(
                    action="password_recovery_requested",
                    entity="password_recovery_request",
                    entity_id=req.id,
                    user_id=user.id,
                    company_id=user.company_id,
                    detail="Solicitud de recuperacion creada desde login.",
                )
                db.session.commit()

        # Mensaje neutro para no revelar si el correo existe o no.
        flash("Solicitud registrada. El administrador la revisara.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", email="")


@bp.route("/force-password-change", methods=["GET", "POST"])
@login_required
def force_password_change():
    from app import PasswordRecoveryRequest, db, record_audit

    if not getattr(current_user, "must_change_password", False):
        return redirect(_post_login_redirect())

    if request.method == "POST":
        new_password = (request.form.get("new_password") or "").strip()
        confirm_password = (request.form.get("confirm_password") or "").strip()

        if len(new_password) < 6:
            flash("La nueva contrasena debe tener al menos 6 caracteres.", "danger")
            return render_template("auth/force_password_change.html")
        if new_password != confirm_password:
            flash("Las contrasenas no coinciden.", "danger")
            return render_template("auth/force_password_change.html")

        current_user.set_password(new_password)
        current_user.must_change_password = False

        pending = (
            PasswordRecoveryRequest.query.filter_by(user_id=current_user.id)
            .filter(PasswordRecoveryRequest.status.in_(["pendiente", "atendida"]))
            .all()
        )
        for item in pending:
            item.status = "cerrada"
            item.processed_at = db.func.now()

        record_audit(
            action="password_changed_after_recovery",
            entity="user",
            entity_id=current_user.id,
            detail="Contrasena actualizada por flujo obligatorio de recuperacion.",
            user_id=current_user.id,
            company_id=current_user.company_id,
        )
        db.session.commit()
        flash("Contrasena actualizada correctamente.", "success")
        return redirect(_post_login_redirect())

    return render_template("auth/force_password_change.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Registro de usuario nuevo."""
    from app import Company, RegisterForm, User, db, record_audit, utcnow
    from services.plan_service import PlanService
    from services.referral_service import ReferralService
    from services.subscription_service import SubscriptionService

    selected_plan_code = (request.values.get("selected_plan") or "trial").strip().lower()
    registration_mode = (request.values.get("mode") or "").strip().lower()

    if request.method == "POST":
        form = RegisterForm()
        if registration_mode == "seller":
            email_candidate = (request.form.get("email") or "").strip().lower()
            if email_candidate:
                existing_user = User.query.filter(db.func.lower(User.email) == email_candidate).first()
                if existing_user is not None:
                    flash("Este correo ya pertenece a un cliente existente. Inicia sesion para activar Referidos desde tu cuenta.", "warning")
                    return redirect(url_for("auth.login", next=url_for("referrals.activate_seller")))
        if form.validate_on_submit():
            if User.query.filter_by(username=form.username.data).first():
                flash("El nombre de usuario ya esta en uso.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

            existing_user = User.query.filter_by(email=form.email.data).first()
            if existing_user:
                if registration_mode == "seller":
                    flash("Este correo ya pertenece a un cliente existente. Inicia sesion para activar Referidos desde tu cuenta.", "warning")
                    return redirect(url_for("auth.login", next=url_for("referrals.activate_seller")))
                flash("Este correo electronico ya esta registrado.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

            if registration_mode == "seller":
                user = User(username=form.username.data, email=form.email.data, auth_provider="local", active=True)
                user.set_password(form.password.data)
                user.role = "seller"
                db.session.add(user)
                db.session.flush()

                profile_data = {
                    "dni": (request.form.get("dni") or "").strip() or f"AUTO-{user.id}",
                    "tax_id": None,
                    "phone": None,
                    "province": None,
                    "city": None,
                    "address": None,
                    "alias": None,
                    "cbu": None,
                    "bank": None,
                    "account_holder": None,
                    "active": True,
                }
                ReferralService.create_or_update_seller(db.session, user=user, profile_data=profile_data)

                record_audit(action="register_seller_success", entity="user", entity_id=user.id, detail="Registro de vendedor exitoso")
                db.session.commit()

                flash("Registro de vendedor exitoso. Inicia sesion para entrar a tu panel.", "success")
                return redirect(url_for("auth.login", next=url_for("referrals.seller_dashboard")))

            company_name = f"Empresa {form.username.data.strip()}"
            company = Company(name=company_name, active=True, trial_ends_at=utcnow() + timedelta(days=10))
            db.session.add(company)
            db.session.flush()

            user = User(username=form.username.data, email=form.email.data, company_id=company.id, auth_provider="local")
            user.set_password(form.password.data)
            user.role = "user"

            db.session.add(user)
            db.session.flush()

            PlanService.ensure_defaults(db.session)
            selected_plan = PlanService.get_plan(code=selected_plan_code) or PlanService.get_plan(code="trial")
            if selected_plan is not None and selected_plan.code != "trial":
                SubscriptionService.start_or_change_plan(
                    db.session,
                    company=company,
                    plan=selected_plan,
                    user_id=user.id,
                )

            referral_code = (
                (request.values.get("ref") or "").strip()
                or (session.get("referral_code") or "").strip()
                or (request.cookies.get("stockarmobile_ref") or "").strip()
            )
            seller = ReferralService.find_seller_by_code(referral_code)
            if seller is not None:
                ReferralService.attribute_company(
                    db.session,
                    seller=seller,
                    company=company,
                    user=user,
                    referral_code=referral_code,
                )

            record_audit(action="register_success", entity="user", entity_id=user.id, detail="Registro de usuario exitoso")
            db.session.commit()

            flash("Registro exitoso. Puedes iniciar sesion ahora.", "success")
            return redirect(url_for("auth.login"))

    form = RegisterForm()
    return render_template("auth/register.html", form=form, selected_plan=selected_plan_code, registration_mode=registration_mode)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """Cerrar sesion."""
    from app import db, record_audit

    if current_user.is_authenticated:
        record_audit(action="logout", entity="user", entity_id=current_user.id, detail="Cierre de sesion")
        db.session.commit()
    logout_user()
    session.clear()
    flash("Has cerrado la sesion.", "info")
    return redirect(url_for("auth.login"))
