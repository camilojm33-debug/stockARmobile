"""
Blueprint de Autenticación: Login, Registro y Google OAuth.
"""

import os
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import login_required, login_user, logout_user

bp = Blueprint('auth', __name__, template_folder='templates')
oauth = OAuth()
_google_oauth_enabled = False


def init_oauth(app):
    global _google_oauth_enabled
    client_id = (app.config.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (app.config.get("GOOGLE_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    _google_oauth_enabled = bool(client_id and client_secret)
    if _google_oauth_enabled:
        oauth.register(
            name="google",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return _google_oauth_enabled


def google_oauth_enabled():
    return _google_oauth_enabled


def _google_client():
    if not _google_oauth_enabled:
        return None
    return oauth.create_client("google")


def _safe_username(base_username):
    from app import User

    candidate = (base_username or "google-user").strip().lower() or "google-user"
    index = 1
    while User.query.filter_by(username=candidate).first() is not None:
        candidate = f"{(base_username or 'google-user').strip().lower()}-{index}"
        index += 1
    return candidate


def _seed_google_company_and_subscription(display_name):
    from app import Company, Plan, Subscription, db, utcnow

    company = Company(
        name=display_name or "StockArmobile",
        active=True,
        trial_ends_at=utcnow() + timedelta(days=10),
    )
    db.session.add(company)
    db.session.flush()

    trial_plan = Plan.query.filter_by(code="trial").first() or Plan.query.order_by(Plan.id.asc()).first()
    if trial_plan is not None:
        db.session.add(
            Subscription(
                company_id=company.id,
                plan_id=trial_plan.id,
                status="trial",
                trial_end=company.trial_ends_at,
                start_date=utcnow(),
                starts_at=utcnow(),
                ends_at=company.trial_ends_at,
                next_billing_date=company.trial_ends_at,
                renewal_enabled=True,
                auto_renew=True,
            )
        )
    return company


def _login_user_and_bind_company(user, remember=False):
    session.clear()
    login_user(user, remember=remember)
    session['company_id'] = user.company_id


def _google_upsert_user(userinfo):
    from app import User, db, generate_password_hash

    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        return None, "No se recibió un correo válido desde Google."
    if not userinfo.get("email_verified"):
        return None, "El correo de Google no fue verificado."

    google_sub = (userinfo.get("sub") or "").strip() or None
    first_name = (userinfo.get("given_name") or "").strip() or None
    last_name = (userinfo.get("family_name") or "").strip() or None
    avatar_url = (userinfo.get("picture") or "").strip() or None
    display_name = (userinfo.get("name") or " ".join(part for part in [first_name, last_name] if part) or email.split("@")[0]).strip()

    user = None
    if google_sub:
        user = User.query.filter_by(google_sub=google_sub).first()
    if user is None:
        user = User.query.filter(db.func.lower(User.email) == email).first()

    if user is None:
        company = _seed_google_company_and_subscription(display_name)
        user = User(
            username=_safe_username(email.split("@")[0]),
            email=email,
            password_hash=generate_password_hash(secrets.token_urlsafe(48)),
            first_name=first_name,
            last_name=last_name,
            avatar_url=avatar_url,
            auth_provider="google",
            google_sub=google_sub,
            role="user",
            active=True,
            company_id=company.id,
        )
        db.session.add(user)
        db.session.commit()
        return user, None

    if user.company_id is None:
        company = _seed_google_company_and_subscription(display_name)
        user.company_id = company.id
    user.first_name = first_name or user.first_name
    user.last_name = last_name or user.last_name
    user.avatar_url = avatar_url or user.avatar_url
    user.google_sub = google_sub or user.google_sub
    user.auth_provider = "google"
    if not user.active:
        user.active = True
    if not user.password_hash:
        user.password_hash = generate_password_hash(secrets.token_urlsafe(48))
    db.session.commit()
    return user, None


def _is_safe_redirect(target):
    if not target:
        return False
    parsed = urlsplit(target)
    return not parsed.netloc and parsed.path.startswith("/")


@bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login de usuario"""
    from app import LoginForm, User, db
    
    if request.method == 'POST':
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
                next_page = request.args.get('next')
                if not _is_safe_redirect(next_page):
                    next_page = None
                flash('Inicio de sesión exitoso', 'success')
                return redirect(next_page if next_page else url_for('dashboard.index'))
            
            flash('Usuario o contraseña incorrectos.', 'danger')
    
    form = LoginForm()
    return render_template('auth/login.html', form=form, google_oauth_enabled=google_oauth_enabled())


@bp.route('/google')
def google_login():
    client = _google_client()
    if client is None:
        return redirect(url_for('auth.login'))
    redirect_uri = url_for('auth.google_callback', _external=True)
    return client.authorize_redirect(redirect_uri)


@bp.route('/google/callback')
def google_callback():
    client = _google_client()
    if client is None:
        return redirect(url_for('auth.login'))

    try:
        client.authorize_access_token()
        userinfo = client.get('userinfo').json()
    except Exception:
        flash('No se pudo completar el inicio de sesión con Google.', 'danger')
        return redirect(url_for('auth.login'))

    user, error = _google_upsert_user(userinfo)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    _login_user_and_bind_company(user, remember=False)
    flash('Inicio de sesión con Google exitoso.', 'success')
    return redirect(url_for('dashboard.index'))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    """Registro de usuario nuevo"""
    from app import Company, RegisterForm, User, db, utcnow
    
    if request.method == 'POST':
        form = RegisterForm()
        if form.validate_on_submit():
            # Verificar si username ya existe
            if User.query.filter_by(username=form.username.data).first():
                flash('El nombre de usuario ya está en uso.', 'danger')
                return redirect(url_for('auth.register'))
            
            # Verificar si email ya está registrado
            if User.query.filter_by(email=form.email.data).first():
                flash('Este correo electrónico ya está registrado.', 'danger')
                return redirect(url_for('auth.register'))
            
            # Cada registro crea su propia empresa para mantener aislamiento tenant.
            company_name = f"Empresa {form.username.data.strip()}"
            company = Company(name=company_name, active=True, trial_ends_at=utcnow() + timedelta(days=10))
            db.session.add(company)
            db.session.flush()

            user = User(username=form.username.data, email=form.email.data, company_id=company.id, auth_provider="local")
            user.set_password(form.password.data)
            user.role = "user"
            
            db.session.add(user)
            db.session.commit()
            
            flash('Registro exitoso. Puedes iniciar sesión ahora.', 'success')
            return redirect(url_for('auth.login'))
    
    form = RegisterForm()
    return render_template('auth/register.html', form=form)


@bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """Cerrar sesión"""
    logout_user()
    session.clear()
    flash('Has cerrado la sesión.', 'info')
    return redirect(url_for('auth.login'))
