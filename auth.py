"""Blueprint de autenticacion: login, registro y recuperacion de contrasena."""

from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import OperationalError, ProgrammingError
from services.password_recovery_service import PasswordRecoveryService
from stockarmobile.constants import SESSION_POST_REGISTER_ONBOARDING_COMPANY_ID
from stockarmobile.helpers.strings import is_safe_relative_redirect

bp = Blueprint("auth", __name__, template_folder="templates")


def _ensure_company_default_branch(company, owner_user):
    """Create Casa central for newly registered tenants and seed their existing stock."""
    from app import Branch, BranchStock, Product, UserBranchAccess, db

    branch = Branch.query.filter_by(company_id=company.id, is_default=True).first()
    if branch is None:
        branch = Branch(
            company_id=company.id,
            name="Casa central",
            code="CASA-CENTRAL",
            address=getattr(company, "address", None),
            city=getattr(company, "city", None),
            province=getattr(company, "province", None),
            phone=getattr(company, "phone", None),
            whatsapp=getattr(company, "whatsapp", None),
            active=True,
            is_default=True,
        )
        db.session.add(branch)
        db.session.flush()
    if owner_user is not None and UserBranchAccess.query.filter_by(
        company_id=company.id, user_id=owner_user.id, branch_id=branch.id
    ).first() is None:
        db.session.add(UserBranchAccess(company_id=company.id, user_id=owner_user.id, branch_id=branch.id))
    products = Product.query.filter_by(company_id=company.id).all()
    existing_rows = {
        row.product_id for row in BranchStock.query.filter_by(company_id=company.id, branch_id=branch.id).all()
    }
    for product in products:
        if product.id not in existing_rows:
            db.session.add(BranchStock(
                company_id=company.id, branch_id=branch.id, product_id=product.id,
                stock=product.stock or 0, min_stock=product.min_stock or 0, reserved_stock=0,
            ))
    return branch


def _login_user_and_bind_company(user, remember=False, company_id=None):
    from stockarmobile.company_memberships import ACTIVE_COMPANY_SESSION_KEY

    preserved_session = {}
    for key in (SESSION_POST_REGISTER_ONBOARDING_COMPANY_ID,):
        if key in session:
            preserved_session[key] = session.get(key)
    selected_company_id = company_id if company_id is not None else getattr(user, "company_id", None)
    session.clear()
    login_user(user, remember=remember)
    session.update({key: value for key, value in preserved_session.items() if value is not None})
    if selected_company_id is not None:
        session[ACTIVE_COMPANY_SESSION_KEY] = int(selected_company_id)


def _post_login_redirect():
    role = getattr(current_user, "role", None)
    if role == "superadmin":
        return url_for("saas.index")
    if role == "seller":
        return url_for("referrals.seller_dashboard")
    return url_for("dashboard.index")


def _is_safe_redirect(target):
    return is_safe_relative_redirect(target)


def _login_mode_from_request():
    mode = (request.values.get("mode") or "").strip().lower()
    next_page = (request.values.get("next") or "").strip()
    if mode == "seller" or next_page.startswith("/referidos"):
        return "seller"
    return "company"


def _registered_company_name(raw_name):
    return f"Empresa {(raw_name or '').strip()}"


def _ensure_demo_account():
    from app import CashSession, Client, Company, Product, Sale, SaleItem, Subscription, User, db, utcnow
    from services.plan_service import PlanService
    from services.subscription_service import SubscriptionService

    PlanService.ensure_defaults(db.session)

    company = Company.query.filter(Company.name == "StockArmobile Demo").first()
    if company is None:
        company = Company(
            name="StockArmobile Demo",
            legal_name="StockArmobile Demo SRL",
            active=True,
            trial_ends_at=utcnow() + timedelta(days=30),
            currency="ARS",
            timezone="America/Argentina/Buenos_Aires",
        )
        db.session.add(company)
        db.session.flush()

    user = User.query.filter_by(username="demo", company_id=company.id).first()
    if user is None:
        user = User(
            username="demo",
            email="demo@stockarmobile.local",
            company_id=company.id,
            auth_provider="local",
            active=True,
            role="admin",
        )
        user.set_password("demo123")
        db.session.add(user)
        db.session.flush()

    subscription = Subscription.query.filter_by(company_id=company.id).first()
    if subscription is None:
        trial_plan = PlanService.get_plan(code="trial")
        SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)

    if Product.query.filter_by(company_id=company.id).count() == 0:
        db.session.add_all(
            [
                Product(barcode="DEMO-001", name="Yerba demo", category="Alimentos", price=18500, cost_price=11000, stock=18, min_stock=5, active=True, sale_type="unidad", unit_measure="u", company_id=company.id),
                Product(barcode="DEMO-002", name="Azucar demo", category="Alimentos", price=2200, cost_price=1350, stock=42, min_stock=10, active=True, sale_type="unidad", unit_measure="u", company_id=company.id),
                Product(barcode="DEMO-003", name="Cafe demo", category="Bebidas", price=7800, cost_price=4100, stock=12, min_stock=4, active=True, sale_type="unidad", unit_measure="u", company_id=company.id),
            ]
        )

    if Client.query.filter_by(company_id=company.id).count() == 0:
        db.session.add_all(
            [
                Client(name="Cliente demo 1", email="cliente1@demo.local", whatsapp="549111111111", active=True, company_id=company.id),
                Client(name="Cliente demo 2", email="cliente2@demo.local", whatsapp="549111111112", active=True, company_id=company.id),
            ]
        )

    if CashSession.query.filter_by(company_id=company.id, status="abierta").first() is None:
        db.session.add(
            CashSession(
                user_id=user.id,
                company_id=company.id,
                opened_by_user_id=user.id,
                opening_amount=0,
                expected_amount=0,
                status="abierta",
                note="Sesión demo",
            )
        )

    if Sale.query.filter_by(company_id=company.id).count() == 0:
        product = Product.query.filter_by(company_id=company.id).first()
        client = Client.query.filter_by(company_id=company.id).first()
        cash_session = CashSession.query.filter_by(company_id=company.id, status="abierta").first()
        if product and client and cash_session:
            sale = Sale(
                date=utcnow(),
                customer=client.name,
                subtotal=product.price,
                total_amount=product.price,
                paid_amount=product.price,
                payment_method="efectivo",
                status="confirmada",
                client_id=client.id,
                seller_id=user.id,
                company_id=company.id,
                cash_session_id=cash_session.id,
                client_txn_id="demo-sale-1",
            )
            db.session.add(sale)
            db.session.flush()
            db.session.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=product.id,
                    quantity=1,
                    price=product.price,
                    cost_price=product.cost_price,
                )
            )
            product.stock = float(product.stock or 0) - 1

    branch = _ensure_company_default_branch(company, user)
    CashSession.query.filter_by(company_id=company.id, status="abierta", branch_id=None).update(
        {"branch_id": branch.id}, synchronize_session=False
    )
    Sale.query.filter_by(company_id=company.id, branch_id=None).update(
        {"branch_id": branch.id}, synchronize_session=False
    )
    db.session.commit()
    return user


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Login de usuario local."""
    from app import LoginForm, User, db, record_audit, utcnow

    login_mode = _login_mode_from_request()

    # Evita que una sesion ya autenticada vea el formulario de login envuelto en el shell
    # del panel (sidebar/data-user-id/data-company-id): se redirige antes de renderizar.
    if request.method == "GET" and current_user.is_authenticated:
        return redirect(_post_login_redirect())

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
                if getattr(user, "role", None) == "superadmin":
                    from saas import _SUPERADMIN_AUTHENTICATED_AT_SESSION_KEY, _SUPERADMIN_STEP_UP_AT_SESSION_KEY, _SUPERADMIN_STEP_UP_USER_SESSION_KEY
                    session[_SUPERADMIN_AUTHENTICATED_AT_SESSION_KEY] = utcnow().timestamp()
                    session.pop(_SUPERADMIN_STEP_UP_AT_SESSION_KEY, None)
                    session.pop(_SUPERADMIN_STEP_UP_USER_SESSION_KEY, None)
                record_audit(action="login_success", entity="user", entity_id=user.id, detail="Inicio de sesion exitoso")
                db.session.commit()
                if getattr(user, "must_change_password", False):
                    flash("Debes cambiar tu contrasena para continuar.", "warning")
                    return redirect(url_for("auth.force_password_change"))
                next_page = (request.form.get("next") or request.args.get("next") or "").strip()
                if not _is_safe_redirect(next_page):
                    next_page = None
                flash("Inicio de sesion exitoso", "success")
                onboarding_company_id = session.get(SESSION_POST_REGISTER_ONBOARDING_COMPANY_ID)
                if onboarding_company_id is not None and onboarding_company_id == getattr(user, "company_id", None):
                    return redirect(url_for("dashboard.onboarding"))
                return redirect(next_page if next_page else _post_login_redirect())

            record_audit(action="login_failed", entity="user", detail=f"Intento de login fallido: {username_or_email}")
            db.session.commit()
            if login_mode == "seller":
                flash("Vendedor o contrasena incorrectos.", "danger")
            else:
                flash("Empresa / Negocio o contrasena incorrectos.", "danger")

    form = LoginForm()
    return render_template("auth/login.html", form=form, login_mode=login_mode)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    from app import PasswordRecoveryRequest, User, db, record_audit, utcnow

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email:
            flash("Debes ingresar un correo electronico.", "danger")
            return render_template("auth/forgot_password.html", email="")

        user = User.query.filter(db.func.lower(User.email) == email).first()
        if user is not None and user.active:
            try:
                PasswordRecoveryService.request_password_reset(db.session, user=user)
                db.session.commit()
            except (OperationalError, ProgrammingError):
                db.session.rollback()
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
                        detail="Solicitud de recuperacion creada desde login (fallback sin tabla de tokens).",
                    )
                    db.session.commit()
                elif existing.status == "atendida":
                    existing.status = "pendiente"
                    existing.requested_at = utcnow()
                    existing.processed_at = None
                    existing.processed_by_user_id = None
                    db.session.commit()

        # Mensaje neutro para no revelar si el correo existe o no.
        flash("Si el correo existe, enviaremos instrucciones para recuperar tu contrasena.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", email="")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    from app import db

    token = (token or "").strip()
    try:
        token_row = PasswordRecoveryService.get_valid_token_row(raw_token=token)
    except (OperationalError, ProgrammingError):
        token_row = None
    if token_row is None:
        flash("El enlace de recuperacion es invalido o expiro.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = (request.form.get("new_password") or "").strip()
        confirm_password = (request.form.get("confirm_password") or "").strip()

        if len(new_password) < 6:
            flash("La nueva contrasena debe tener al menos 6 caracteres.", "danger")
            return render_template("auth/reset_password.html", token=token)
        if new_password != confirm_password:
            flash("Las contrasenas no coinciden.", "danger")
            return render_template("auth/reset_password.html", token=token)

        user = PasswordRecoveryService.consume_token_and_set_password(
            db.session,
            raw_token=token,
            new_password=new_password,
        )
        if user is None:
            db.session.rollback()
            flash("El enlace de recuperacion es invalido o expiro.", "danger")
            return redirect(url_for("auth.forgot_password"))

        db.session.commit()
        flash("Contrasena actualizada correctamente. Ya puedes iniciar sesion.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


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


@bp.route("/activar-empresa/<token>", methods=["GET", "POST"])
def activate_commercial(token):
    """One-time activation form for a paid Comercial IA signup."""
    import hashlib
    from app import SaaSCommercialCheckout, db
    from services.saas_commercial_whatsapp import _utcnow

    token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    checkout = (
        SaaSCommercialCheckout.query
        .filter_by(activation_token_hash=token_hash, status="activated")
        .filter(SaaSCommercialCheckout.user_id.isnot(None))
        .order_by(SaaSCommercialCheckout.id.desc())
        .first()
    )
    if checkout is None or checkout.user is None or checkout.company is None:
        return render_template("auth/commercial_activation.html", valid=False), 404

    if request.method == "POST":
        password = str(request.form.get("password") or "")
        password_confirm = str(request.form.get("password_confirm") or "")
        if len(password) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "danger")
            return render_template("auth/commercial_activation.html", valid=True, checkout=checkout), 400
        if password != password_confirm:
            flash("Las contraseñas no coinciden.", "danger")
            return render_template("auth/commercial_activation.html", valid=True, checkout=checkout), 400

        user = checkout.user
        user.set_password(password)
        user.must_change_password = False
        user.active = True
        checkout.activation_token_hash = None
        checkout.activation_token_encrypted = None
        checkout.updated_at = _utcnow()
        db.session.commit()
        _login_user_and_bind_company(user, remember=True)
        flash("Tu empresa fue activada. Bienvenido a StockArMobile.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("auth/commercial_activation.html", valid=True, checkout=checkout)


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Registro de usuario nuevo."""
    from app import Company, ReferralSeller, RegisterForm, User, db, record_audit, utcnow
    from services.plan_service import PlanService
    from services.referral_service import ReferralService
    from services.saas_ops_service import SaaSOpsService
    from services.subscription_service import SubscriptionService

    selected_plan_code = (request.values.get("selected_plan") or "trial").strip().lower()
    registration_mode = (request.values.get("mode") or "").strip().lower()

    if request.method == "POST":
        form = RegisterForm()
        if registration_mode == "seller":
            email_candidate = (request.form.get("email") or "").strip().lower()
            dni_candidate = (request.form.get("dni") or "").strip()
            if email_candidate:
                existing_user = User.query.filter(db.func.lower(User.email) == email_candidate).first()
                if existing_user is not None:
                    flash("Este correo ya pertenece a un cliente existente. Inicia sesion para activar Referidos desde tu cuenta.", "warning")
                    return redirect(url_for("auth.login", next=url_for("referrals.activate_seller")))
            if not dni_candidate:
                flash("El DNI es obligatorio.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))
            if ReferralSeller.query.filter(db.func.lower(ReferralSeller.dni) == dni_candidate.lower()).first() is not None:
                flash("Este DNI ya tiene una cuenta de vendedor registrada.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))
        if form.validate_on_submit():
            company_name = _registered_company_name(form.username.data)
            existing_company = Company.query.filter(db.func.lower(Company.name) == company_name.lower()).first()
            if existing_company is not None and registration_mode != "seller":
                flash("Ya existe una empresa con ese nombre.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

            existing_user = User.query.filter(db.func.lower(User.email) == form.email.data.strip().lower()).first()

            if registration_mode != "seller" and existing_user is not None:
                if existing_user.company_id is None and (existing_user.role or "") != "superadmin":
                    if not existing_user.active:
                        flash("Este usuario no esta activo. Contacta a soporte para continuar.", "danger")
                        return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))
                    if not existing_user.check_password(form.password.data):
                        flash("La contrasena no coincide con el usuario existente.", "danger")
                        return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

                    PlanService.ensure_defaults(db.session)
                    trial_plan = PlanService.get_plan(code="trial")
                    try:
                        company = Company(name=company_name, active=True, trial_ends_at=utcnow() + timedelta(days=10))
                        db.session.add(company)
                        db.session.flush()

                        existing_user.company_id = company.id
                        existing_user.role = "admin"
                        existing_user.auth_provider = existing_user.auth_provider or "local"

                        SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)

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
                                user=existing_user,
                                referral_code=referral_code,
                            )

                        _ensure_company_default_branch(company, existing_user)
                        record_audit(action="register_existing_user_company_success", entity="user", entity_id=existing_user.id, detail="Usuario existente convertido en propietario de empresa")
                        SaaSOpsService.register_signup(db.session, company=company, user=existing_user)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                        raise

                    session["post_register_onboarding_company_id"] = company.id
                    flash("Registro exitoso. Puedes iniciar sesion ahora.", "success")
                    return redirect(url_for("auth.login"))

                flash("Este usuario ya tiene una empresa registrada.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

            if User.query.filter_by(username=form.username.data).first():
                if registration_mode == "seller":
                    flash("El nombre de vendedor ya esta en uso.", "danger")
                else:
                    flash("Ese usuario ya existe.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

            if existing_user:
                if registration_mode == "seller":
                    flash("Este correo ya pertenece a un cliente existente. Inicia sesion para activar Referidos desde tu cuenta.", "warning")
                    return redirect(url_for("auth.login", next=url_for("referrals.activate_seller")))
                flash("Este usuario ya tiene una empresa registrada.", "danger")
                return redirect(url_for("auth.register", selected_plan=selected_plan_code, mode=registration_mode))

            if registration_mode == "seller":
                user = User(username=form.username.data, email=form.email.data, auth_provider="local", active=True)
                user.set_password(form.password.data)
                user.role = "seller"
                db.session.add(user)
                db.session.flush()

                profile_data = {
                    "dni": dni_candidate,
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

            PlanService.ensure_defaults(db.session)
            trial_plan = PlanService.get_plan(code="trial")
            try:
                company = Company(name=company_name, active=True, trial_ends_at=utcnow() + timedelta(days=10))
                db.session.add(company)
                db.session.flush()

                user = User(username=form.username.data, email=form.email.data, company_id=company.id, auth_provider="local")
                user.set_password(form.password.data)
                # Defensa adicional: solo el primer usuario de la empresa queda como admin.
                is_first_company_user = User.query.filter_by(company_id=company.id).count() == 0
                user.role = "admin" if is_first_company_user else "user"

                db.session.add(user)
                db.session.flush()

                SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)

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

                _ensure_company_default_branch(company, user)
                record_audit(action="register_success", entity="user", entity_id=user.id, detail="Registro de usuario exitoso")
                SaaSOpsService.register_signup(db.session, company=company, user=user)
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise

            session["post_register_onboarding_company_id"] = company.id

            flash("Registro exitoso. Puedes iniciar sesion ahora.", "success")
            return redirect(url_for("auth.login"))

    form = RegisterForm()
    return render_template("auth/register.html", form=form, selected_plan=selected_plan_code, registration_mode=registration_mode)


@bp.route("/demo", methods=["GET"])
def demo():
    demo_user = _ensure_demo_account()
    _login_user_and_bind_company(demo_user, remember=False)
    session["demo_mode"] = True
    session["guided_tour_pending"] = True
    session["guided_tour_seen"] = False
    flash("Entraste al modo demo con datos ficticios.", "info")
    return redirect(url_for("dashboard.index"))


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


@bp.route("/switch-user", methods=["POST"])
@login_required
def switch_user():
    from app import User, db, record_audit

    if getattr(current_user, "role", None) != "admin":
        abort(403)

    company_id = getattr(current_user, "company_id", None)
    if company_id is None:
        abort(403)

    target_user_id = request.form.get("target_user_id", type=int)
    from stockarmobile.company_memberships import get_company_users
    target_user = next(
        (
            item for item in get_company_users(company_id, active_only=True)
            if item.id == target_user_id and item.role in {"user", "admin"}
        ),
        None,
    )
    if target_user is None:
        flash("Empleado no encontrado o no disponible para cambio de sesión.", "warning")
        return redirect(url_for("dashboard.index"))

    actor_id = current_user.id
    actor_username = current_user.username

    _login_user_and_bind_company(target_user, remember=False, company_id=company_id)
    record_audit(
        action="session_switch_user",
        entity="user",
        entity_id=target_user.id,
        user_id=actor_id,
        company_id=company_id,
        detail=f"Cambio de sesión desde {actor_username} hacia {target_user.username}",
    )
    db.session.commit()

    next_page = (request.form.get("next") or "").strip()
    if not _is_safe_redirect(next_page):
        next_page = None
    flash(f"Sesión cambiada a {target_user.username}.", "success")
    return redirect(next_page if next_page else _post_login_redirect())


@bp.route("/switch-company", methods=["POST"])
@login_required
def switch_company():
    """Switch tenant only when the authenticated identity has an active membership."""
    from app import Company, CompanyMembership, db, record_audit
    from stockarmobile.branch_context import SESSION_KEY as ACTIVE_BRANCH_SESSION_KEY
    from stockarmobile.company_memberships import ACTIVE_COMPANY_SESSION_KEY, apply_membership_context

    try:
        target_company_id = int(request.form.get("company_id") or 0)
    except (TypeError, ValueError):
        abort(400)
    membership = CompanyMembership.query.filter_by(
        user_id=current_user.id,
        company_id=target_company_id,
        active=True,
        status="active",
    ).first()
    company = Company.query.filter_by(id=target_company_id, active=True).first()
    if membership is None or company is None:
        abort(403)

    previous_company_id = getattr(current_user, "company_id", None)
    apply_membership_context(current_user, membership)
    session[ACTIVE_COMPANY_SESSION_KEY] = int(target_company_id)
    session.pop(ACTIVE_BRANCH_SESSION_KEY, None)
    session.modified = True
    record_audit(
        action="company_membership_switch",
        entity="company",
        entity_id=target_company_id,
        company_id=target_company_id,
        detail=f"Cambio de empresa desde company_id={previous_company_id} hacia company_id={target_company_id}.",
    )
    db.session.commit()
    # Flask-Login caches the identity on the application context. Invalidate
    # that cache so the next request reloads the membership selected in session.
    from flask import g
    g.pop("_login_user", None)
    g.pop("_stockarmobile_membership_context_key", None)
    flash(f"Empresa activa: {company.name}.", "success")
    return redirect(url_for("dashboard.index"))


@bp.route("/company-invitations/<token>/accept", methods=["GET", "POST"])
@login_required
def accept_company_invitation(token):
    """Accept a one-time invitation after authenticating with the invited email."""
    import hashlib
    from app import Branch, Company, CompanyMembership, User, UserBranchAccess, db, record_audit, utcnow
    from stockarmobile.branch_context import SESSION_KEY as ACTIVE_BRANCH_SESSION_KEY
    from stockarmobile.company_memberships import ACTIVE_COMPANY_SESSION_KEY, apply_membership_context

    token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    membership = CompanyMembership.query.filter_by(
        invitation_token_hash=token_hash,
        status="invited",
        active=False,
    ).first()
    if membership is None or membership.invitation_expires_at is None or membership.invitation_expires_at <= utcnow():
        flash("La invitación no existe o venció. Pedí que te envíen una nueva.", "warning")
        return redirect(url_for("dashboard.index"))

    invited_user = db.session.get(User, membership.user_id)
    company = db.session.get(Company, membership.company_id)
    if invited_user is None or company is None or not company.active:
        abort(404)
    if int(invited_user.id) != int(current_user.id) or (invited_user.email or "").strip().lower() != (current_user.email or "").strip().lower():
        abort(403)
    if request.method == "GET":
        return render_template("auth/company_invitation.html", company=company, invited_email=invited_user.email, token=token)

    membership.status = "active"
    membership.active = True
    membership.accepted_at = utcnow()
    membership.invitation_token_hash = None
    membership.invitation_expires_at = None
    apply_membership_context(invited_user, membership)

    default_branch = Branch.query.filter_by(
        company_id=company.id,
        is_default=True,
        active=True,
    ).first()
    if default_branch is not None and UserBranchAccess.query.filter_by(
        company_id=company.id,
        user_id=invited_user.id,
        branch_id=default_branch.id,
    ).first() is None:
        db.session.add(UserBranchAccess(
            company_id=company.id,
            user_id=invited_user.id,
            branch_id=default_branch.id,
        ))

    session[ACTIVE_COMPANY_SESSION_KEY] = int(company.id)
    session.pop(ACTIVE_BRANCH_SESSION_KEY, None)
    session.modified = True
    record_audit(
        action="company_membership_accepted",
        entity="company",
        entity_id=company.id,
        company_id=company.id,
        detail=f"Invitación de acceso aceptada por usuario_id={invited_user.id}.",
    )
    db.session.commit()
    # Reload the authenticated identity on the following request using the
    # newly accepted membership, rather than retaining a pre-invitation cache.
    from flask import g
    g.pop("_login_user", None)
    g.pop("_stockarmobile_membership_context_key", None)
    flash(f"Ya tenés acceso a {company.name}.", "success")
    return redirect(url_for("dashboard.index"))
