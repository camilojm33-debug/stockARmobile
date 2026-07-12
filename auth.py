"""
Blueprint de Autenticación: Login y Registro
"""

from datetime import timedelta
from urllib.parse import urlsplit

from flask import Blueprint, render_template, redirect, request, session, url_for, flash
from flask_login import login_required, login_user, logout_user

bp = Blueprint('auth', __name__, template_folder='templates')


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
                login_user(user)
                session['company_id'] = user.company_id
                next_page = request.args.get('next')
                if not _is_safe_redirect(next_page):
                    next_page = None
                flash('Inicio de sesión exitoso', 'success')
                return redirect(next_page if next_page else url_for('dashboard.index'))
            
            flash('Usuario o contraseña incorrectos.', 'danger')
    
    form = LoginForm()
    return render_template('auth/login.html', form=form)


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

            user = User(username=form.username.data, email=form.email.data, company_id=company.id)
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
    session.pop('company_id', None)
    logout_user()
    flash('Has cerrado la sesión.', 'info')
    return redirect(url_for('auth.login'))
