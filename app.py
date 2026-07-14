"""
StockArmobile - aplicacion Flask de inventario, ventas, clientes y QR.
Compatible con SQLite local, PostgreSQL/Render y Flask-Login.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from decimal import Decimal

from flask import Flask, abort, flash, g, jsonify, make_response, redirect, render_template, request, send_from_directory, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect, FlaskForm
from sqlalchemy import Index, inspect, text
from sqlalchemy.exc import ProgrammingError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from wtforms import BooleanField, DateField, DecimalField, PasswordField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional
from config.logging_config import configure_logging
from services.notification_service import build_notifications
from services.search_service import global_search

try:
    from psycopg2.errors import UndefinedTable as PGUndefinedTable
except ImportError:  # pragma: no cover - unavailable outside postgres runtime
    PGUndefinedTable = None


class _NeverUndefinedTableError(Exception):
    pass


UndefinedTableError = PGUndefinedTable or _NeverUndefinedTableError


configure_logging()
app = Flask(__name__, template_folder="templates", static_folder="static")
sys.modules.setdefault("app", sys.modules[__name__])
is_production_env = os.environ.get("FLASK_ENV") == "production" or bool(os.environ.get("RENDER"))
is_pytest_context = "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))
secret_key = os.environ.get("SECRET_KEY")
if is_production_env and not is_pytest_context and not secret_key:
    raise RuntimeError("SECRET_KEY es obligatorio en produccion.")
app.config["SECRET_KEY"] = secret_key or "stockarmobile-dev-secret"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["WTF_CSRF_TIME_LIMIT"] = None
if is_production_env:
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https"

database_url = os.environ.get("DATABASE_URL", "sqlite:///stock_armobile.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Official support contact channels centralized for full-platform reuse.
app.config["SUPPORT_EMAIL"] = (os.environ.get("SUPPORT_EMAIL") or os.environ.get("LANDING_EMAIL") or "stockarmobile@gmail.com").strip()
app.config["SUPPORT_WHATSAPP_DISPLAY"] = (os.environ.get("SUPPORT_WHATSAPP_DISPLAY") or os.environ.get("LANDING_WHATSAPP") or "+54 9 3624 22-8296").strip()
app.config["SUPPORT_WHATSAPP_NUMBER"] = (
    os.environ.get("SUPPORT_WHATSAPP_NUMBER")
    or "".join(ch for ch in app.config["SUPPORT_WHATSAPP_DISPLAY"] if ch.isdigit())
    or "5493624228296"
).strip()
app.config["COMPANY_PIN_SESSION_TTL_MINUTES"] = int(os.environ.get("COMPANY_PIN_SESSION_TTL_MINUTES", "30"))
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
csrf = CSRFProtect(app)

MONEY = db.Numeric(18, 2)
PERCENT = db.Numeric(10, 4)

login_manager = LoginManager(app)
login_manager.login_view = "auth.login"
login_manager.login_message = "Debes iniciar sesion para acceder a esta pagina."
login_manager.login_message_category = "info"


def utcnow():
    """Return UTC now without tzinfo for compatibility with current DB columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "font-src 'self' data: https:; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https: wss:; "
        "worker-src 'self' blob:; "
        "frame-src 'self' https://*.mercadopago.com; "
        "frame-ancestors 'self';",
    )
    if is_production_env:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_current_company_id():
    if not current_user.is_authenticated:
        return None
    if getattr(current_user, "role", None) == "superadmin":
        return None
    return getattr(current_user, "company_id", None)


def scope_query_to_company(query, model):
    if not current_user.is_authenticated:
        return query
    if getattr(current_user, "role", None) == "superadmin":
        return query
    company_id = get_current_company_id()
    if company_id is None or not hasattr(model, "company_id"):
        return query
    return query.filter(model.company_id == company_id)


def is_control_panel_owner(user):
    owner_username = (os.environ.get("ADMIN_USERNAME") or "admin").strip().lower()
    owner_email = (os.environ.get("ADMIN_EMAIL") or "admin@stockarmobile.local").strip().lower()
    username = (getattr(user, "username", None) or "").strip().lower()
    email = (getattr(user, "email", None) or "").strip().lower()
    return username == owner_username or email == owner_email


def tenant_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, "role", None) == "superadmin":
            flash("El panel de empresa no está disponible para SuperAdmin.", "warning")
            return redirect(url_for("saas.index"))
        company_id = get_current_company_id()
        if company_id is None:
            if request.is_json or request.path.startswith("/api"):
                return jsonify({"error": "No hay contexto de empresa activo."}), 403
            flash("No hay contexto de empresa activo para esta sesión.", "warning")
            return redirect(url_for("auth.login"))
        state = get_company_access_state(company_id)
        if not state["can_access"]:
            if request.is_json or request.path.startswith("/api"):
                return jsonify({"error": state["reason"]}), 403
            return redirect(url_for("access_status"))
        return func(*args, **kwargs)
    return decorated


def superadmin_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, "role", None) != "superadmin":
            abort(403)
        return func(*args, **kwargs)
    return decorated


def company_admin_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            abort(403)
        if get_current_company_id() is None:
            abort(403)
        return func(*args, **kwargs)

    return decorated


def seller_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, "role", None) == "seller":
            return func(*args, **kwargs)
        if getattr(current_user, "role", None) == "superadmin":
            abort(403)
        profile = ReferralSeller.query.filter_by(user_id=current_user.id, active=True).first()
        if profile is None:
            if request.is_json or request.path.startswith("/api"):
                abort(403)
            flash("Activa tu Programa de Referidos para acceder al portal.", "info")
            return redirect(url_for("referrals.activate_seller"))
        return func(*args, **kwargs)

    return decorated


def trial_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, "role", None) == "superadmin":
            flash("El panel de empresa no está disponible para SuperAdmin.", "warning")
            return redirect(url_for("saas.index"))
        company_id = get_current_company_id()
        if company_id is None:
            return redirect(url_for("auth.login"))
        state = get_company_access_state(company_id)
        if not state["can_access"]:
            abort(403)
        return func(*args, **kwargs)
    return decorated


def get_company_access_state(company_id):
    from app import Company, Subscription

    if company_id is None:
        return {"status": "missing", "can_access": False, "reason": "No hay empresa activa."}
    company = db.session.get(Company, company_id)
    if company is None:
        return {"status": "missing", "can_access": False, "reason": "Empresa no encontrada."}
    if not company.active:
        return {"status": "suspended", "can_access": False, "reason": "La empresa ha sido suspendida."}
    subscription = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.starts_at.desc()).first()
    if subscription is None:
        if company.trial_ends_at and utcnow() > company.trial_ends_at:
            return {"status": "trial_expired", "can_access": False, "reason": "El periodo de prueba finalizó."}
        return {"status": "trial", "can_access": True, "reason": "Periodo de prueba activo."}

    trial_limit = subscription.trial_end or company.trial_ends_at
    if trial_limit and utcnow() > trial_limit and (subscription.status or "").lower() == "trial":
        return {"status": "trial_expired", "can_access": False, "reason": "Tu prueba expiró. Suscribite para continuar."}

    status = (subscription.status or "trial").lower()
    if status in {"suspended", "expired", "cancelled", "canceled", "rejected", "charged_back"}:
        return {"status": status, "can_access": False, "reason": "La suscripción no está activa."}
    if status in {"pending", "pending_payment", "in_process", "authorized"}:
        return {"status": status, "can_access": False, "reason": "Pago pendiente de confirmación."}
    if status in {"trial", "active", "activa", "trialing", "approved"}:
        return {"status": status, "can_access": True, "reason": "Suscripción activa."}
    return {"status": status, "can_access": True, "reason": "Estado de suscripción reconocido."}


@app.before_request
def bind_tenant_context():
    if current_user.is_authenticated:
        if getattr(current_user, "role", None) == "superadmin":
            company_id = None
        else:
            company_id = getattr(current_user, "company_id", None)
        g.current_company_id = company_id
    else:
        g.current_company_id = None


@app.before_request
def enforce_password_change_if_required():
    if not current_user.is_authenticated:
        return None
    if not getattr(current_user, "must_change_password", False):
        return None

    endpoint = request.endpoint or ""
    allowed = {
        "auth.force_password_change",
        "auth.logout",
        "static",
    }
    if endpoint in allowed:
        return None
    return redirect(url_for("auth.force_password_change"))


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    first_name = db.Column(db.String(80))
    last_name = db.Column(db.String(80))
    avatar_url = db.Column(db.String(255))
    auth_provider = db.Column(db.String(30), default="local")
    google_sub = db.Column(db.String(120), unique=True, index=True)
    role = db.Column(db.String(20), default="user")
    active = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def name(self):
        full_name = " ".join(part for part in [self.first_name, self.last_name] if part)
        return full_name or self.username

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Product(db.Model):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_company_barcode", "company_id", "barcode", unique=True),
    )

    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(100))
    sale_type = db.Column(db.String(30), default="unidad")
    unit_measure = db.Column(db.String(20), default="u")
    photo = db.Column(db.String(255))
    brand = db.Column(db.String(120))
    supplier = db.Column(db.String(160))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    cost_price = db.Column(MONEY, default=Decimal("0.00"))
    price = db.Column(MONEY, default=Decimal("0.00"))
    margin = db.Column(MONEY, default=Decimal("0.00"))
    profit_percent = db.Column(PERCENT, default=Decimal("0.0000"))
    tax = db.Column(PERCENT, default=Decimal("0.0000"))
    stock = db.Column(db.Float, default=0.0)
    min_stock = db.Column(db.Float, default=5.0)
    discount = db.Column(MONEY, default=Decimal("0.00"))
    favorite = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def code(self):
        return self.barcode

    @property
    def codigo(self):
        return self.barcode

    @property
    def nombre(self):
        return self.name

    @property
    def categoria(self):
        return self.category

    @property
    def category_name(self):
        return self.category

    @property
    def sku(self):
        return self.barcode

    @property
    def precio_costo(self):
        return self.cost_price

    @property
    def precio_venta(self):
        return self.price

    @property
    def tipo_venta(self):
        return self.sale_type

    @property
    def unidad_medida(self):
        return self.unit_measure

    @property
    def profit_amount(self):
        return float(self.price or 0) - float(self.cost_price or 0)

    @property
    def iva(self):
        return self.tax

    def __repr__(self):
        return f"<Product {self.name}>"


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20), index=True)
    address = db.Column(db.Text)
    city = db.Column(db.String(100))
    notes = db.Column(db.Text)
    whatsapp = db.Column(db.String(30))
    birthday = db.Column(db.Date)
    balance = db.Column(MONEY, default=Decimal("0.00"))
    credit_limit = db.Column(MONEY, default=Decimal("0.00"))
    observations = db.Column(db.Text)
    account_current_enabled = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def nombre(self):
        return self.name

    @property
    def telefono(self):
        return self.phone


class Sale(db.Model):
    __tablename__ = "sales"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=utcnow, index=True)
    customer = db.Column(db.String(200))
    subtotal = db.Column(MONEY, default=Decimal("0.00"))
    discount = db.Column(MONEY, default=Decimal("0.00"))
    tax = db.Column(MONEY, default=Decimal("0.00"))
    total_amount = db.Column(MONEY, default=Decimal("0.00"))
    payment_method = db.Column(db.String(50))
    secondary_payment_method = db.Column(db.String(50))
    paid_amount = db.Column(MONEY, default=Decimal("0.00"))
    secondary_paid_amount = db.Column(MONEY, default=Decimal("0.00"))
    surcharge = db.Column(MONEY, default=Decimal("0.00"))
    document_type = db.Column(db.String(30), default="venta")
    status = db.Column(db.String(30), default="confirmada", index=True)
    qr_reference = db.Column(db.String(160))
    note = db.Column(db.Text)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"))
    seller_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    client = db.relationship("Client", backref="sales")
    seller = db.relationship("User", backref="sales")
    items = db.relationship("SaleItem", backref="sale", lazy=True, cascade="all, delete-orphan")
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def products(self):
        return self.items


class SaleItem(db.Model):
    __tablename__ = "sale_items"

    id = db.Column(db.Integer, primary_key=True)
    quantity = db.Column(db.Float, nullable=False)
    price = db.Column(MONEY, nullable=False)
    cost_price = db.Column(MONEY, default=Decimal("0.00"))
    discount = db.Column(MONEY, default=Decimal("0.00"))
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product = db.relationship("Product", backref="sale_items")

    @property
    def total_amount(self):
        unit_price = self.price or Decimal("0.00")
        quantity = Decimal(str(self.quantity or 0))
        discount = self.discount or Decimal("0.00")
        gross = unit_price * quantity
        return max(gross - discount, Decimal("0.00"))


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, default="StockArmobile")
    legal_name = db.Column(db.String(160))
    address = db.Column(db.String(255))
    phone = db.Column(db.String(40))
    contact_email = db.Column(db.String(160), index=True)
    tax_id = db.Column(db.String(50))
    payment_alias = db.Column(db.String(120))
    payment_cbu = db.Column(db.String(40))
    payment_cvu = db.Column(db.String(40))
    payment_qr_text = db.Column(db.String(255))
    payment_qr_url = db.Column(db.String(255))
    logo = db.Column(db.String(255))
    business_pin_hash = db.Column(db.String(255))
    business_pin_failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    business_pin_blocked_until = db.Column(db.DateTime)
    business_pin_updated_at = db.Column(db.DateTime)
    active = db.Column(db.Boolean, default=True, nullable=False)
    trial_ends_at = db.Column(db.DateTime)
    license_key = db.Column(db.String(120), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Plan(db.Model):
    __tablename__ = "plans"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(MONEY, default=Decimal("0.00"))
    currency = db.Column(db.String(10), default="ARS")
    duration_days = db.Column(db.Integer, default=30)
    max_users = db.Column(db.Integer, default=1)
    max_products = db.Column(db.Integer, default=1000)
    max_clients = db.Column(db.Integer, default=1000)
    features_json = db.Column(db.Text)
    state = db.Column(db.String(20), default="active", index=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class Subscription(db.Model):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_company_status", "company_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"))
    status = db.Column(db.String(30), default="trial", index=True)
    starts_at = db.Column(db.DateTime, default=utcnow)
    ends_at = db.Column(db.DateTime)
    trial_end = db.Column(db.DateTime)
    start_date = db.Column(db.DateTime, default=utcnow)
    next_billing_date = db.Column(db.DateTime, index=True)
    last_payment_date = db.Column(db.DateTime)
    cancel_at_period_end = db.Column(db.Boolean, default=False, nullable=False)
    renewal_enabled = db.Column(db.Boolean, default=True, nullable=False)
    mercadopago_subscription_id = db.Column(db.String(120), index=True)
    auto_renew = db.Column(db.Boolean, default=True, nullable=False)
    external_reference = db.Column(db.String(120), index=True)
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    company = db.relationship("Company", backref="subscriptions")
    plan = db.relationship("Plan")


class Invoice(db.Model):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_company_status", "company_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"))
    status = db.Column(db.String(30), default="draft", index=True)
    amount = db.Column(MONEY, default=Decimal("0.00"))
    vat_amount = db.Column(MONEY, default=Decimal("0.00"))
    currency = db.Column(db.String(10), default="USD")
    due_at = db.Column(db.DateTime)
    issued_at = db.Column(db.DateTime, default=utcnow)
    paid_at = db.Column(db.DateTime)
    invoice_number = db.Column(db.String(80), unique=True, index=True)
    detail = db.Column(db.Text)
    line_items_json = db.Column(db.Text)
    provider = db.Column(db.String(40), default="manual")
    reference = db.Column(db.String(120))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    company = db.relationship("Company", backref="invoices")
    subscription = db.relationship("Subscription", backref="invoices")


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_company_status", "company_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.String(120), unique=True, index=True)
    preference_id = db.Column(db.String(120), index=True)
    external_reference = db.Column(db.String(120), index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    amount = db.Column(MONEY, default=Decimal("0.00"))
    currency = db.Column(db.String(10), default="USD")
    status = db.Column(db.String(30), default="pending", index=True)
    payment_method = db.Column(db.String(80))
    provider = db.Column(db.String(40), default="manual")
    reference = db.Column(db.String(120))
    paid_at = db.Column(db.DateTime)
    payload_json = db.Column(db.Text)
    next_billing_date = db.Column(db.DateTime)
    last_payment_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    company = db.relationship("Company", backref="payments")
    subscription = db.relationship("Subscription", backref="payments")
    user = db.relationship("User")
    invoice = db.relationship("Invoice", backref="payments")


class PaymentHistory(db.Model):
    __tablename__ = "payment_history"
    __table_args__ = (
        Index("ix_payment_history_company_event", "company_id", "event"),
    )

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"))
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    event = db.Column(db.String(80), nullable=False)
    detail = db.Column(db.Text)
    source = db.Column(db.String(40), default="system")
    event_id = db.Column(db.String(120), index=True)
    payload_json = db.Column(db.Text)
    status = db.Column(db.String(30), index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    payment = db.relationship("Payment", backref="history")
    subscription = db.relationship("Subscription")
    invoice = db.relationship("Invoice")
    company = db.relationship("Company", backref="payment_history")


class WebhookEvent(db.Model):
    __tablename__ = "webhook_events"

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), nullable=False, default="mercadopago")
    event_key = db.Column(db.String(180), nullable=False, unique=True, index=True)
    event_type = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(30), default="processed", index=True)
    payload_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    whatsapp = db.Column(db.String(30))
    address = db.Column(db.Text)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class PurchaseOrder(db.Model):
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    date = db.Column(db.DateTime, default=utcnow, index=True)
    status = db.Column(db.String(30), default="recibida", index=True)
    subtotal = db.Column(MONEY, default=Decimal("0.00"))
    total_amount = db.Column(MONEY, default=Decimal("0.00"))
    note = db.Column(db.Text)
    supplier = db.relationship("Supplier", backref="purchase_orders")
    items = db.relationship("PurchaseItem", backref="purchase_order", cascade="all, delete-orphan")


class PurchaseItem(db.Model):
    __tablename__ = "purchase_items"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(MONEY, nullable=False)
    product = db.relationship("Product")

    @property
    def total_amount(self):
        return (self.quantity or 0) * (self.unit_cost or 0)


class CashSession(db.Model):
    __tablename__ = "cash_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    opened_at = db.Column(db.DateTime, default=utcnow, index=True)
    closed_at = db.Column(db.DateTime)
    opening_amount = db.Column(MONEY, default=Decimal("0.00"))
    closing_amount = db.Column(MONEY)
    expected_amount = db.Column(MONEY, default=Decimal("0.00"))
    status = db.Column(db.String(20), default="abierta", index=True)
    note = db.Column(db.Text)
    user = db.relationship("User")
    movements = db.relationship("CashMovement", backref="session", cascade="all, delete-orphan")


class CashMovement(db.Model):
    __tablename__ = "cash_movements"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("cash_sessions.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    movement_type = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(80))
    amount = db.Column(MONEY, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    user = db.relationship("User")


class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=utcnow, index=True)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(240), nullable=False)
    amount = db.Column(MONEY, nullable=False)
    payment_method = db.Column(db.String(50))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    user = db.relationship("User")


class ProductPriceHistory(db.Model):
    __tablename__ = "product_price_history"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    old_price = db.Column(MONEY, default=Decimal("0.00"))
    new_price = db.Column(MONEY, default=Decimal("0.00"))
    old_cost = db.Column(MONEY, default=Decimal("0.00"))
    new_cost = db.Column(MONEY, default=Decimal("0.00"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    product = db.relationship("Product", backref="price_history")


class ProductModification(db.Model):
    __tablename__ = "product_modifications"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(80), nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    product = db.relationship("Product", backref="modifications")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    action = db.Column(db.String(120), nullable=False)
    entity = db.Column(db.String(80))
    entity_id = db.Column(db.Integer)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class BackupLog(db.Model):
    __tablename__ = "backup_logs"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    status = db.Column(db.String(30), default="pendiente")
    path = db.Column(db.String(255))
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class PasswordRecoveryRequest(db.Model):
    __tablename__ = "password_recovery_requests"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    email = db.Column(db.String(160), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="pendiente", index=True)
    requested_at = db.Column(db.DateTime, default=utcnow, index=True)
    processed_at = db.Column(db.DateTime)
    processed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company", foreign_keys=[company_id], backref="password_recovery_requests")
    user = db.relationship("User", foreign_keys=[user_id], backref="password_recovery_requests")
    processed_by = db.relationship("User", foreign_keys=[processed_by_user_id], backref="password_recovery_processed")


class ReferralSeller(db.Model):
    __tablename__ = "referral_sellers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    dni = db.Column(db.String(30), nullable=False)
    tax_id = db.Column(db.String(30))
    phone = db.Column(db.String(30))
    province = db.Column(db.String(120))
    city = db.Column(db.String(120))
    address = db.Column(db.String(255))
    alias = db.Column(db.String(120))
    cbu = db.Column(db.String(22))
    bank = db.Column(db.String(120))
    account_holder = db.Column(db.String(160))
    referral_code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    referral_url = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", backref="seller_profile")


class ReferralAttribution(db.Model):
    __tablename__ = "referral_attributions"

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    referral_code = db.Column(db.String(20), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    seller = db.relationship("ReferralSeller", backref="attributions")
    company = db.relationship("Company", backref="referral_attribution")
    user = db.relationship("User", backref="referral_attributions")


class ReferralCommission(db.Model):
    __tablename__ = "referral_commissions"

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    attribution_id = db.Column(db.Integer, db.ForeignKey("referral_attributions.id"), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"), index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"), index=True)
    sold_amount = db.Column(MONEY, default=Decimal("0.00"), nullable=False)
    commission_percent = db.Column(PERCENT, default=Decimal("0.3000"), nullable=False)
    commission_amount = db.Column(MONEY, default=Decimal("0.00"), nullable=False)
    status = db.Column(db.String(20), default="pendiente", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    available_at = db.Column(db.DateTime, index=True)
    paid_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    note = db.Column(db.Text)

    seller = db.relationship("ReferralSeller", backref="commissions")
    attribution = db.relationship("ReferralAttribution", backref="commissions")
    company = db.relationship("Company", backref="referral_commissions")
    subscription = db.relationship("Subscription", backref="referral_commissions")
    payment = db.relationship("Payment", backref="referral_commissions")
    plan = db.relationship("Plan")


class ReferralPayout(db.Model):
    __tablename__ = "referral_payouts"

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    processed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount = db.Column(MONEY, default=Decimal("0.00"), nullable=False)
    transfer_date = db.Column(db.DateTime, nullable=False)
    receipt = db.Column(db.String(255))
    transfer_number = db.Column(db.String(120))
    observations = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    seller = db.relationship("ReferralSeller", backref="payouts")
    processed_by = db.relationship("User")


class ReferralPayoutItem(db.Model):
    __tablename__ = "referral_payout_items"

    id = db.Column(db.Integer, primary_key=True)
    payout_id = db.Column(db.Integer, db.ForeignKey("referral_payouts.id"), nullable=False, index=True)
    commission_id = db.Column(db.Integer, db.ForeignKey("referral_commissions.id"), nullable=False, unique=True, index=True)

    payout = db.relationship("ReferralPayout", backref="items")
    commission = db.relationship("ReferralCommission", backref="payout_item")


class LandingTestimonial(db.Model):
    __tablename__ = "landing_testimonials"

    id = db.Column(db.Integer, primary_key=True)
    author_name = db.Column(db.String(120), nullable=False)
    company_name = db.Column(db.String(160))
    quote = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class SupportTicket(db.Model):
    __tablename__ = "support_tickets"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    email = db.Column(db.String(160), nullable=False)
    reason = db.Column(db.String(80), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pendiente", index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    resolved_at = db.Column(db.DateTime)
    resolved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    resolved_note = db.Column(db.Text)

    company = db.relationship("Company", foreign_keys=[company_id], backref="support_tickets")
    user = db.relationship("User", foreign_keys=[user_id], backref="support_tickets_created")
    resolved_by = db.relationship("User", foreign_keys=[resolved_by_user_id], backref="support_tickets_resolved")


def record_audit(*, action, entity=None, entity_id=None, detail=None, user_id=None, company_id=None):
    try:
        db.session.add(
            AuditLog(
                user_id=user_id if user_id is not None else (current_user.id if current_user.is_authenticated else None),
                company_id=company_id if company_id is not None else get_current_company_id(),
                action=action,
                entity=entity,
                entity_id=entity_id,
                detail=detail,
            )
        )
    except Exception:
        app.logger.exception("No se pudo registrar auditoria: %s", action)


class RegisterForm(FlaskForm):
    username = StringField("Usuario", validators=[DataRequired(), Length(min=3, max=50)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Contrasena", validators=[DataRequired(), Length(min=6)])
    submit = SubmitField("Registrarse")


class LoginForm(FlaskForm):
    username = StringField("Usuario / Email", validators=[DataRequired()])
    password = PasswordField("Contrasena", validators=[DataRequired()])
    remember = BooleanField("Recordarme")
    submit = SubmitField("Iniciar sesion")


class ProductForm(FlaskForm):
    barcode = StringField("Codigo", validators=[Optional(), Length(max=50)])
    name = StringField("Nombre", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Descripcion", validators=[Optional()])
    category = StringField("Categoria", validators=[Optional(), Length(max=100)])
    sale_type = SelectField(
        "Tipo de venta",
        choices=[
            ("unidad", "Unidad"),
            ("kilogramo", "Kilogramo"),
            ("gramos", "Gramos"),
            ("litros", "Litros"),
            ("mililitros", "Mililitros"),
            ("metros", "Metros"),
        ],
        default="unidad",
    )
    unit_measure = StringField("Unidad medida", validators=[Optional(), Length(max=20)], default="u")
    brand = StringField("Marca", validators=[Optional(), Length(max=120)])
    supplier = StringField("Proveedor", validators=[Optional(), Length(max=160)])
    cost_price = DecimalField("Precio costo", validators=[Optional(), NumberRange(min=0)], default=0)
    price = DecimalField("Precio venta", validators=[Optional(), NumberRange(min=0)], default=0)
    margin = DecimalField("Ganancia $", validators=[Optional(), NumberRange(min=0)], default=0)
    profit_percent = DecimalField("Margen %", validators=[Optional(), NumberRange(min=0)], default=0)
    tax = DecimalField("IVA %", validators=[Optional(), NumberRange(min=0)], default=0)
    stock = DecimalField("Stock", validators=[Optional(), NumberRange(min=0)], default=0)
    min_stock = DecimalField("Stock minimo", validators=[Optional(), NumberRange(min=0)], default=5)
    discount = DecimalField("Descuento", validators=[Optional(), NumberRange(min=0)], default=0)
    favorite = BooleanField("Favorito")
    submit = SubmitField("Guardar")

    @property
    def codigo(self):
        return self.barcode

    @property
    def nombre(self):
        return self.name

    @property
    def categoria(self):
        return self.category

    @property
    def precio_costo(self):
        return self.cost_price

    @property
    def precio_venta(self):
        return self.price

    @property
    def iva(self):
        return self.tax


class ClientForm(FlaskForm):
    name = StringField("Nombre", validators=[DataRequired(), Length(max=200)])
    email = StringField("Email", validators=[Optional(), Email()])
    phone = StringField("Telefono", validators=[Optional(), Length(max=20)])
    whatsapp = StringField("WhatsApp", validators=[Optional(), Length(max=30)])
    birthday = DateField("Cumpleanos", validators=[Optional()])
    balance = DecimalField("Saldo", validators=[Optional()], default=0)
    credit_limit = DecimalField("Limite de credito", validators=[Optional(), NumberRange(min=0)], default=0)
    address = TextAreaField("Direccion", validators=[Optional()])
    city = StringField("Ciudad", validators=[Optional(), Length(max=100)])
    notes = TextAreaField("Notas", validators=[Optional()])
    observations = TextAreaField("Observaciones", validators=[Optional()])
    account_current_enabled = BooleanField("Cuenta corriente")
    submit = SubmitField("Guardar")


import auth  # noqa: E402
import cash  # noqa: E402
import clients  # noqa: E402
import dashboard  # noqa: E402
import expenses  # noqa: E402
import products  # noqa: E402
import purchases  # noqa: E402
import qr_labels  # noqa: E402
import reports  # noqa: E402
import saas  # noqa: E402
import company_billing  # noqa: E402
import referrals  # noqa: E402
import sales  # noqa: E402
import support  # noqa: E402

auth_bp = auth.bp
dashboard_bp = dashboard.bp
products_bp = products.bp
clients_bp = clients.bp
sales_bp = sales.bp
qr_labels_bp = qr_labels.bp
purchases_bp = purchases.bp
cash_bp = cash.bp
expenses_bp = expenses.bp
reports_bp = reports.bp
saas_bp = saas.bp
company_billing_bp = company_billing.bp
referrals_bp = referrals.bp
support_bp = support.bp

app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
app.register_blueprint(products_bp, url_prefix="/productos")
app.register_blueprint(clients_bp, url_prefix="/clientes")
app.register_blueprint(sales_bp, url_prefix="/ventas")
app.register_blueprint(qr_labels_bp, url_prefix="/qr")
app.register_blueprint(purchases_bp, url_prefix="/compras")
app.register_blueprint(cash_bp, url_prefix="/caja")
app.register_blueprint(expenses_bp, url_prefix="/gastos")
app.register_blueprint(reports_bp, url_prefix="/reportes")
app.register_blueprint(saas_bp, url_prefix="/superadmin")
app.register_blueprint(company_billing_bp, url_prefix="/admin")
app.register_blueprint(referrals_bp)
app.register_blueprint(support_bp, url_prefix="/soporte")


def _plan_feature_flags(plan):
    raw = (getattr(plan, "features_json", "") or "").strip().lower()
    tokens = {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}
    has_all = "all" in tokens

    def has(*names):
        return has_all or any(name in tokens for name in names)

    return {
        "usuarios": int(getattr(plan, "max_users", 0) or 0),
        "productos": int(getattr(plan, "max_products", 0) or 0),
        "clientes": int(getattr(plan, "max_clients", 0) or 0),
        "ventas": has("ventas"),
        "caja": has("caja"),
        "reportes": has("reportes", "reportes_basicos"),
        "qr": has("qr"),
        "etiquetas": has("etiquetas", "excel", "kardex"),
        "whatsapp": has("whatsapp"),
        "multiusuario": int(getattr(plan, "max_users", 0) or 0) > 1,
        "soporte": True,
    }


@app.route("/")
def index():
    from services.plan_service import PlanService
    from services.referral_service import ReferralService
    from services.subscription_service import SubscriptionService
    from app import LandingTestimonial, ReferralAttribution, ReferralCommission, ReferralSeller

    PlanService.ensure_defaults(db.session)
    plans = PlanService.all_commercial_plans()

    current_plan_id = None
    if current_user.is_authenticated and getattr(current_user, "role", None) != "superadmin":
        company_id = getattr(current_user, "company_id", None)
        if company_id:
            subscription = SubscriptionService.active_subscription_for_company(company_id)
            current_plan_id = getattr(subscription, "plan_id", None)

    trial_plan = next((plan for plan in plans if (plan.code or "").lower() == "trial"), None)
    paid_plans = [plan for plan in plans if float(plan.price or 0) > 0]
    recommended_plan = None
    if current_plan_id:
        recommended_plan = next((plan for plan in plans if plan.id == current_plan_id), None)
    if recommended_plan is None:
        if paid_plans:
            recommended_plan = paid_plans[len(paid_plans) // 2]
        elif plans:
            recommended_plan = plans[0]

    plan_feature_rows = {plan.id: _plan_feature_flags(plan) for plan in plans}
    referral_percent = float(ReferralService.COMMISSION_PERCENT)

    ranking_rows = []

    def _is_undefined_table(error):
        if PGUndefinedTable is None:
            return False
        if isinstance(error, PGUndefinedTable):
            return True
        return isinstance(getattr(error, "orig", None), PGUndefinedTable)

    try:
        ranking_rows = (
            db.session.query(
                ReferralSeller,
                db.func.coalesce(db.func.sum(ReferralCommission.sold_amount), 0).label("sold_total"),
                db.func.coalesce(db.func.count(db.distinct(ReferralAttribution.company_id)), 0).label("clients_total"),
            )
            .outerjoin(ReferralCommission, ReferralCommission.seller_id == ReferralSeller.id)
            .outerjoin(ReferralAttribution, ReferralAttribution.seller_id == ReferralSeller.id)
            .group_by(ReferralSeller.id)
            .order_by(db.text("sold_total DESC"))
            .limit(3)
            .all()
        )
    except ProgrammingError as exc:
        if not _is_undefined_table(exc):
            raise
        db.session.rollback()
        app.logger.warning("Landing cargada sin ranking de referidos por tabla inexistente: %s", exc)
    except UndefinedTableError as exc:
        db.session.rollback()
        app.logger.warning("Landing cargada sin ranking de referidos por UndefinedTable: %s", exc)

    medal_targets = [1, 5, 10, 25, 50, 100]
    medal_progress = []
    for row in ranking_rows:
        clients_total = int(row.clients_total or 0)
        medal_progress.append(
            {
                "seller": row[0],
                "clients_total": clients_total,
                "medals": [target for target in medal_targets if clients_total >= target],
                "sold_total": float(row.sold_total or 0),
            }
        )

    app_base_url = (os.environ.get("APP_URL") or request.url_root.rstrip("/")).rstrip("/")
    seo = {
        "title": "StockArmobile | Controla tu negocio desde cualquier lugar",
        "description": "Ventas, Stock, Clientes, Caja, QR, Etiquetas y Reportes en una sola plataforma con prueba gratuita y programa profesional de referidos.",
        "url": f"{app_base_url}/",
        "image": f"{app_base_url}{url_for('static', filename='assets/icons/icon-512.png')}",
        "site_name": "StockArmobile",
    }

    whatsapp_value = app.config.get("SUPPORT_WHATSAPP_DISPLAY", "+54 9 3624 22-8296")
    whatsapp_digits = app.config.get("SUPPORT_WHATSAPP_NUMBER", "5493624228296")
    contact = {
        "whatsapp": whatsapp_value,
        "whatsapp_link": f"https://wa.me/{whatsapp_digits}" if whatsapp_digits else "https://wa.me/",
        "email": app.config.get("SUPPORT_EMAIL", "stockarmobile@gmail.com"),
    }
    demo_video_url = (os.environ.get("LANDING_DEMO_VIDEO_URL") or "").strip()

    testimonials = []
    try:
        testimonials = (
            LandingTestimonial.query.filter(LandingTestimonial.active.is_(True))
            .order_by(LandingTestimonial.created_at.desc())
            .limit(6)
            .all()
        )
    except ProgrammingError as exc:
        if not _is_undefined_table(exc):
            raise
        db.session.rollback()
        app.logger.warning("Landing cargada sin testimonios por tabla inexistente: %s", exc)
    except UndefinedTableError as exc:
        db.session.rollback()
        app.logger.warning("Landing cargada sin testimonios por UndefinedTable: %s", exc)

    referral_code = (request.args.get("ref") or "").strip().upper()
    response = make_response(
        render_template(
            "landing/index.html",
            plans=plans,
            current_plan_id=current_plan_id,
            recommended_plan_id=getattr(recommended_plan, "id", None),
            trial_plan=trial_plan,
            plan_feature_rows=plan_feature_rows,
            referral_percent=referral_percent,
            ranking_rows=ranking_rows,
            medal_targets=medal_targets,
            medal_progress=medal_progress,
            testimonials=testimonials,
            seo=seo,
            contact=contact,
            demo_video_url=demo_video_url,
        )
    )
    if referral_code:
        seller = ReferralService.find_seller_by_code(referral_code)
        if seller is not None:
            record_audit(
                action="referral_link_click",
                entity="referral_seller",
                entity_id=seller.id,
                detail=f"Click registrado para codigo {referral_code}.",
                user_id=seller.user_id,
                company_id=getattr(getattr(seller, "user", None), "company_id", None),
            )
            db.session.commit()
        session["referral_code"] = referral_code
        response.set_cookie("stockarmobile_ref", referral_code, max_age=60 * 60 * 24 * 90, samesite="Lax")
    return response


@app.route("/landing/contact", methods=["POST"])
def landing_contact():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    message = (request.form.get("message") or "").strip()

    if not name or not email or not message:
        flash("Completa nombre, email y mensaje para enviarnos tu consulta.", "warning")
        return redirect(url_for("index", _anchor="contacto"))

    support_email = app.config.get("SUPPORT_EMAIL", "stockarmobile@gmail.com")
    app.logger.info(
        "Lead landing contacto: to=%s name=%s email=%s message_len=%s",
        support_email,
        name,
        email,
        len(message),
    )
    flash(
        "Gracias por comunicarte con StockArmobile. Nuestro equipo respondera tu consulta a la brevedad.",
        "success",
    )
    return redirect(url_for("index", _anchor="contacto"))


@app.route("/access-status")
def access_status():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    company_id = get_current_company_id()
    if company_id is None:
        return redirect(url_for("auth.login"))
    state = get_company_access_state(company_id)
    return render_template("errors/access_status.html", state=state)


@app.errorhandler(404)
def not_found(error):
    return render_template("errors/404.html"), 404


@app.errorhandler(403)
def forbidden(error):
    return render_template("errors/403.html"), 403


@app.errorhandler(500)
def internal_error(error):
    app.logger.exception("Error interno no controlado: %s", error)
    return render_template("errors/500.html"), 500


@app.context_processor
def inject_notifications():
    has_active_seller_profile = False
    support_contact = {
        "email": app.config.get("SUPPORT_EMAIL", "stockarmobile@gmail.com"),
        "whatsapp_display": app.config.get("SUPPORT_WHATSAPP_DISPLAY", "+54 9 3624 22-8296"),
        "whatsapp_number": app.config.get("SUPPORT_WHATSAPP_NUMBER", "5493624228296"),
        "whatsapp_link": f"https://wa.me/{app.config.get('SUPPORT_WHATSAPP_NUMBER', '5493624228296')}",
        "email_link": f"mailto:{app.config.get('SUPPORT_EMAIL', 'stockarmobile@gmail.com')}",
    }
    if current_user.is_authenticated:
        notifications = build_notifications()
        if getattr(current_user, "role", None) != "superadmin":
            has_active_seller_profile = ReferralSeller.query.filter_by(user_id=current_user.id, active=True).first() is not None
        return {
            "notification_items": notifications,
            "notification_count": len(notifications),
            "has_active_seller_profile": has_active_seller_profile,
            "support_contact": support_contact,
        }
    return {
        "notification_items": [],
        "notification_count": 0,
        "has_active_seller_profile": False,
        "support_contact": support_contact,
    }


@app.route("/api/search")
@login_required
def api_search():
    return jsonify({"results": global_search(request.args.get("q", ""))})


@app.route("/api/notifications")
@login_required
def api_notifications():
    return jsonify({"notifications": build_notifications()})


@app.route("/manifest.json")
def web_manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/offline.html")
def offline_page():
    return send_from_directory(app.static_folder, "offline.html", mimetype="text/html")


def create_admin_user():
    from services.plan_service import PlanService

    admin_created = False
    admin_updated = False
    company = Company.query.first()
    if company is None:
        company = Company(name=os.environ.get("COMPANY_NAME", "StockArmobile"))
        db.session.add(company)
        db.session.flush()
    if company.trial_ends_at is None:
        company.trial_ends_at = utcnow() + timedelta(days=10)
    PlanService.ensure_defaults(db.session)

    admin_username = (os.environ.get("ADMIN_USERNAME", "admin") or "admin").strip() or "admin"
    admin_email = (os.environ.get("ADMIN_EMAIL", "admin@stockarmobile.local") or "admin@stockarmobile.local").strip().lower()
    by_username = User.query.filter_by(username=admin_username).first()
    by_email = User.query.filter_by(email=admin_email).first()

    is_production = os.environ.get("FLASK_ENV") == "production" or os.environ.get("RENDER")
    admin_password = os.environ.get("ADMIN_PASSWORD")

    target_admin = by_email or by_username
    if target_admin is not None:
        changed = False
        if by_email is not None:
            # Priorizamos el email para evitar IntegrityError cuando OAuth ya creó ese usuario.
            username_taken_by_other = by_username is not None and by_username.id != by_email.id
            if not username_taken_by_other and target_admin.username != admin_username:
                target_admin.username = admin_username
                changed = True
            elif username_taken_by_other and target_admin.username != admin_username:
                app.logger.warning("No se pudo normalizar username admin por conflicto con otro usuario existente.")
        desired_role = target_admin.role or "user"
        if desired_role != "superadmin":
            desired_role = "superadmin" if is_control_panel_owner(target_admin) else "admin"
        if target_admin.role != desired_role:
            target_admin.role = desired_role
            changed = True
        if not target_admin.active:
            target_admin.active = True
            changed = True
        if target_admin.company_id is None:
            target_admin.company_id = company.id
            changed = True
        if changed:
            admin_updated = True
    else:
        if is_production and not admin_password:
            app.logger.warning("ADMIN_PASSWORD no configurado; no se pudo crear el usuario admin.")
        else:
            user = User(
                username=admin_username,
                email=admin_email,
                company_id=company.id,
                active=True,
            )
            user.set_password(admin_password or "admin123")
            user.role = "superadmin"
            db.session.add(user)
            admin_created = True

    if Subscription.query.filter_by(company_id=company.id).first() is None:
        trial_plan = Plan.query.filter_by(code="trial").first() or Plan.query.order_by(Plan.id.asc()).first()
        subscription = Subscription(
            company_id=company.id,
            plan_id=trial_plan.id if trial_plan else None,
            status="trial",
            trial_end=company.trial_ends_at,
            start_date=utcnow(),
            starts_at=utcnow(),
            ends_at=company.trial_ends_at,
            next_billing_date=company.trial_ends_at,
            renewal_enabled=True,
            auto_renew=True,
        )
        db.session.add(subscription)
    db.session.commit()
    if admin_created:
        app.logger.info("Admin creado")
    elif admin_updated:
        app.logger.info("Admin actualizado")
    return admin_created


def bootstrap_database():
    """Bootstrap opcional de datos base usando migraciones."""
    if getattr(app, "_bootstrap_done", False):
        return
    if (os.environ.get("ENABLE_BOOTSTRAP") or "").strip().lower() != "true":
        app.logger.info("Bootstrap deshabilitado. Usa ENABLE_BOOTSTRAP=true para habilitarlo.")
        app._bootstrap_done = True
        return
    with app.app_context():
        from flask_migrate import upgrade

        app.logger.info("Aplicando migraciones por bootstrap...")
        upgrade()
        create_admin_user()
        ensure_primary_superadmin()
        app.logger.info("Bootstrap completado")
    app._bootstrap_done = True


def ensure_primary_superadmin():
    """Garantiza que el primer usuario tenga acceso total al sistema."""
    first_user = User.query.order_by(User.id.asc()).first()
    if first_user is None:
        return

    changed = False
    company = Company.query.first()
    if company is None:
        company = Company(name=os.environ.get("COMPANY_NAME", "StockArmobile"), active=True)
        db.session.add(company)
        db.session.flush()

    if first_user.company_id is None:
        first_user.company_id = company.id
        changed = True
    if not first_user.active:
        first_user.active = True
        changed = True
    expected_role = first_user.role or "user"
    if expected_role != "superadmin" and is_control_panel_owner(first_user):
        expected_role = "superadmin"
    if first_user.role != expected_role:
        first_user.role = expected_role
        changed = True

    if changed:
        db.session.commit()


def ensure_database_schema():
    """Compatibilidad retroactiva: deshabilitado para forzar migraciones Alembic."""
    app.logger.info("ensure_database_schema() deshabilitado. Usa migraciones Alembic.")


@app.cli.command("init-db")
def init_db_command():
    from flask_migrate import upgrade

    upgrade()
    create_admin_user()
    ensure_primary_superadmin()
    app.logger.info("Base de datos inicializada correctamente.")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

