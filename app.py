"""
StockArmobile - aplicacion Flask de inventario, ventas, clientes y QR.
Compatible con SQLite local, PostgreSQL/Render y Flask-Login.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect, FlaskForm
from sqlalchemy import Index, inspect, text
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from wtforms import BooleanField, DateField, DecimalField, PasswordField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional
from config.logging_config import configure_logging
from services.notification_service import build_notifications
from services.search_service import global_search


configure_logging()
app = Flask(__name__, template_folder="templates", static_folder="static")
sys.modules.setdefault("app", sys.modules[__name__])
is_production_env = os.environ.get("FLASK_ENV") == "production" or bool(os.environ.get("RENDER"))
secret_key = os.environ.get("SECRET_KEY")
if is_production_env and not secret_key:
    secret_key = os.environ.get("SECRET_KEY", "stockarmobile-temporary-secret")
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
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
csrf = CSRFProtect(app)

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
    cost_price = db.Column(db.Float, default=0.0)
    price = db.Column(db.Float, default=0.0)
    margin = db.Column(db.Float, default=0.0)
    profit_percent = db.Column(db.Float, default=0.0)
    stock = db.Column(db.Float, default=0.0)
    min_stock = db.Column(db.Float, default=5.0)
    discount = db.Column(db.Float, default=0.0)
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
    balance = db.Column(db.Float, default=0.0)
    credit_limit = db.Column(db.Float, default=0.0)
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
    subtotal = db.Column(db.Float, default=0.0)
    discount = db.Column(db.Float, default=0.0)
    tax = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(50))
    secondary_payment_method = db.Column(db.String(50))
    paid_amount = db.Column(db.Float, default=0.0)
    secondary_paid_amount = db.Column(db.Float, default=0.0)
    surcharge = db.Column(db.Float, default=0.0)
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
    price = db.Column(db.Float, nullable=False)
    cost_price = db.Column(db.Float, default=0.0)
    discount = db.Column(db.Float, default=0.0)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product = db.relationship("Product", backref="sale_items")

    @property
    def total_amount(self):
        gross = (self.price or 0) * (self.quantity or 0)
        return max(gross - (self.discount or 0), 0)


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, default="StockArmobile")
    contact_email = db.Column(db.String(160), index=True)
    tax_id = db.Column(db.String(50))
    logo = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True, nullable=False)
    trial_ends_at = db.Column(db.DateTime)
    license_key = db.Column(db.String(120), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Plan(db.Model):
    __tablename__ = "plans"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, default=0.0)
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
    amount = db.Column(db.Float, default=0.0)
    vat_amount = db.Column(db.Float, default=0.0)
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
    amount = db.Column(db.Float, default=0.0)
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
    subtotal = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)
    note = db.Column(db.Text)
    supplier = db.relationship("Supplier", backref="purchase_orders")
    items = db.relationship("PurchaseItem", backref="purchase_order", cascade="all, delete-orphan")


class PurchaseItem(db.Model):
    __tablename__ = "purchase_items"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)
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
    opening_amount = db.Column(db.Float, default=0.0)
    closing_amount = db.Column(db.Float)
    expected_amount = db.Column(db.Float, default=0.0)
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
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    user = db.relationship("User")


class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=utcnow, index=True)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(240), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(50))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    user = db.relationship("User")


class ProductPriceHistory(db.Model):
    __tablename__ = "product_price_history"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"))
    old_price = db.Column(db.Float, default=0.0)
    new_price = db.Column(db.Float, default=0.0)
    old_cost = db.Column(db.Float, default=0.0)
    new_cost = db.Column(db.Float, default=0.0)
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
    price = DecimalField("Precio venta", validators=[DataRequired(), NumberRange(min=0)], default=0)
    margin = DecimalField("Margen", validators=[Optional(), NumberRange(min=0)], default=0)
    profit_percent = DecimalField("% ganancia", validators=[Optional(), NumberRange(min=0)], default=0)
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
import sales  # noqa: E402

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

auth.init_oauth(app)

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


@app.route("/")
def index():
    return render_template("landing/index.html")


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
    if current_user.is_authenticated:
        notifications = build_notifications()
        return {"notification_items": notifications, "notification_count": len(notifications)}
    return {"notification_items": [], "notification_count": 0}


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
    admin_created = False
    admin_updated = False
    company = Company.query.first()
    if company is None:
        company = Company(name=os.environ.get("COMPANY_NAME", "StockArmobile"))
        db.session.add(company)
        db.session.flush()
    if company.trial_ends_at is None:
        company.trial_ends_at = utcnow() + timedelta(days=10)

    existing_codes = {plan.code for plan in Plan.query.all() if plan.code}
    plan_seed = [
        {"code": "trial", "name": "Trial", "price": 0.0, "currency": "ARS", "duration_days": 10, "max_users": 2, "max_products": 150, "max_clients": 250, "features_json": "inventario,ventas,clientes,reportes_basicos", "state": "active"},
        {"code": "entrepreneur", "name": "Emprendedor", "price": 12999.0, "currency": "ARS", "duration_days": 30, "max_users": 3, "max_products": 1200, "max_clients": 2000, "features_json": "inventario,ventas,clientes,reportes,excel", "state": "active"},
        {"code": "business", "name": "Negocio", "price": 29999.0, "currency": "ARS", "duration_days": 30, "max_users": 8, "max_products": 12000, "max_clients": 12000, "features_json": "inventario,ventas,clientes,compras,caja,reportes,excel,kardex", "state": "active"},
        {"code": "premium", "name": "Premium", "price": 54999.0, "currency": "ARS", "duration_days": 30, "max_users": 50, "max_products": 100000, "max_clients": 100000, "features_json": "all", "state": "active"},
    ]
    if not existing_codes:
        for payload in plan_seed:
            db.session.add(Plan(**payload))
    else:
        for payload in plan_seed:
            if payload["code"] not in existing_codes:
                db.session.add(Plan(**payload))

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
    """Crea esquema, indices y seeds esenciales al arrancar por WSGI o CLI."""
    if getattr(app, "_bootstrap_done", False):
        return
    with app.app_context():
        app.logger.info("Creando tablas...")
        db.create_all()
        inspector = inspect(db.engine)
        if "users" in set(inspector.get_table_names()):
            app.logger.info("Tabla users creada")
        else:
            app.logger.error("Tabla users no encontrada luego de create_all")
        ensure_database_schema()
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
    expected_role = "superadmin" if is_control_panel_owner(first_user) else (first_user.role or "user")
    if first_user.role != expected_role:
        first_user.role = expected_role
        changed = True

    if changed:
        db.session.commit()


def ensure_database_schema():
    """Agrega columnas nuevas en bases existentes sin romper SQLite/PostgreSQL."""
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    column_specs = {
        "users": {
            "active": "BOOLEAN DEFAULT TRUE",
            "company_id": "INTEGER",
            "first_name": "VARCHAR(80)",
            "last_name": "VARCHAR(80)",
            "avatar_url": "VARCHAR(255)",
            "auth_provider": "VARCHAR(30) DEFAULT 'local'",
            "google_sub": "VARCHAR(120)",
        },
        "companies": {
            "contact_email": "VARCHAR(160)",
        },
        "products": {
            "sale_type": "VARCHAR(30) DEFAULT 'unidad'",
            "unit_measure": "VARCHAR(20) DEFAULT 'u'",
            "photo": "VARCHAR(255)",
            "brand": "VARCHAR(120)",
            "supplier": "VARCHAR(160)",
            "supplier_id": "INTEGER",
            "margin": "DOUBLE PRECISION DEFAULT 0",
            "profit_percent": "DOUBLE PRECISION DEFAULT 0",
            "favorite": "BOOLEAN DEFAULT FALSE",
            "company_id": "INTEGER",
        },
        "clients": {
            "whatsapp": "VARCHAR(30)",
            "birthday": "DATE",
            "balance": "DOUBLE PRECISION DEFAULT 0",
            "credit_limit": "DOUBLE PRECISION DEFAULT 0",
            "observations": "TEXT",
            "account_current_enabled": "BOOLEAN DEFAULT FALSE",
            "company_id": "INTEGER",
        },
        "sales": {
            "secondary_payment_method": "VARCHAR(50)",
            "paid_amount": "DOUBLE PRECISION DEFAULT 0",
            "secondary_paid_amount": "DOUBLE PRECISION DEFAULT 0",
            "surcharge": "DOUBLE PRECISION DEFAULT 0",
            "document_type": "VARCHAR(30) DEFAULT 'venta'",
            "status": "VARCHAR(30) DEFAULT 'confirmada'",
            "qr_reference": "VARCHAR(160)",
            "company_id": "INTEGER",
        },
        "sale_items": {
            "cost_price": "DOUBLE PRECISION DEFAULT 0",
            "discount": "DOUBLE PRECISION DEFAULT 0",
        },
        "suppliers": {
            "company_id": "INTEGER",
        },
        "purchase_orders": {
            "company_id": "INTEGER",
        },
        "expenses": {
            "company_id": "INTEGER",
        },
        "cash_sessions": {
            "company_id": "INTEGER",
        },
        "cash_movements": {
            "company_id": "INTEGER",
        },
        "product_price_history": {
            "company_id": "INTEGER",
        },
        "product_modifications": {
            "company_id": "INTEGER",
        },
        "audit_logs": {
            "company_id": "INTEGER",
        },
        "backup_logs": {
            "company_id": "INTEGER",
        },
        "plans": {
            "code": "VARCHAR(40)",
            "currency": "VARCHAR(10) DEFAULT 'ARS'",
            "duration_days": "INTEGER DEFAULT 30",
            "max_clients": "INTEGER DEFAULT 1000",
            "features_json": "TEXT",
            "state": "VARCHAR(20) DEFAULT 'active'",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
        "subscriptions": {
            "trial_end": "DATETIME",
            "start_date": "DATETIME",
            "next_billing_date": "DATETIME",
            "last_payment_date": "DATETIME",
            "cancel_at_period_end": "BOOLEAN DEFAULT FALSE",
            "renewal_enabled": "BOOLEAN DEFAULT TRUE",
            "mercadopago_subscription_id": "VARCHAR(120)",
            "external_reference": "VARCHAR(120)",
            "metadata_json": "TEXT",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
        "invoices": {
            "company_id": "INTEGER",
            "subscription_id": "INTEGER",
            "status": "VARCHAR(30) DEFAULT 'draft'",
            "amount": "DOUBLE PRECISION DEFAULT 0",
            "vat_amount": "DOUBLE PRECISION DEFAULT 0",
            "currency": "VARCHAR(10) DEFAULT 'USD'",
            "due_at": "DATETIME",
            "issued_at": "DATETIME",
            "paid_at": "DATETIME",
            "invoice_number": "VARCHAR(80)",
            "detail": "TEXT",
            "line_items_json": "TEXT",
            "provider": "VARCHAR(40) DEFAULT 'manual'",
            "reference": "VARCHAR(120)",
            "note": "TEXT",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
        "payments": {
            "payment_id": "VARCHAR(120)",
            "preference_id": "VARCHAR(120)",
            "external_reference": "VARCHAR(120)",
            "company_id": "INTEGER",
            "subscription_id": "INTEGER",
            "user_id": "INTEGER",
            "invoice_id": "INTEGER",
            "amount": "DOUBLE PRECISION DEFAULT 0",
            "currency": "VARCHAR(10) DEFAULT 'USD'",
            "status": "VARCHAR(30) DEFAULT 'pending'",
            "payment_method": "VARCHAR(80)",
            "provider": "VARCHAR(40) DEFAULT 'manual'",
            "reference": "VARCHAR(120)",
            "paid_at": "DATETIME",
            "payload_json": "TEXT",
            "next_billing_date": "DATETIME",
            "last_payment_date": "DATETIME",
            "updated_at": "DATETIME",
        },
        "payment_history": {
            "payment_id": "INTEGER",
            "subscription_id": "INTEGER",
            "invoice_id": "INTEGER",
            "company_id": "INTEGER",
            "event": "VARCHAR(80)",
            "detail": "TEXT",
            "source": "VARCHAR(40)",
            "event_id": "VARCHAR(120)",
            "payload_json": "TEXT",
            "status": "VARCHAR(30)",
        },
        "webhook_events": {
            "provider": "VARCHAR(40) DEFAULT 'mercadopago'",
            "event_key": "VARCHAR(180)",
            "event_type": "VARCHAR(80)",
            "status": "VARCHAR(30)",
            "payload_json": "TEXT",
            "created_at": "DATETIME",
        },
    }
    with db.engine.begin() as connection:
        for table_name, specs in column_specs.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl_type in specs.items():
                if column_name not in existing_columns:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))
        if db.engine.dialect.name == "postgresql" and "products" in existing_tables:
            connection.execute(text("ALTER TABLE products ALTER COLUMN stock TYPE DOUBLE PRECISION USING stock::double precision"))
            connection.execute(text("ALTER TABLE products ALTER COLUMN min_stock TYPE DOUBLE PRECISION USING min_stock::double precision"))
            connection.execute(text("ALTER TABLE sale_items ALTER COLUMN quantity TYPE DOUBLE PRECISION USING quantity::double precision"))

        index_statements = [
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_products_company_barcode ON products(company_id, barcode)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub ON users(google_sub)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_plans_code ON plans(code)",
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_company_status ON subscriptions(company_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_next_billing_date ON subscriptions(next_billing_date)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_invoices_invoice_number ON invoices(invoice_number)",
            "CREATE INDEX IF NOT EXISTS ix_invoices_company_status ON invoices(company_id, status)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_payment_id ON payments(payment_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_external_reference ON payments(external_reference)",
            "CREATE INDEX IF NOT EXISTS ix_payments_company_status ON payments(company_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_payment_history_company_event ON payment_history(company_id, event)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_webhook_events_event_key ON webhook_events(event_key)",
        ]
        for stmt in index_statements:
            try:
                connection.execute(text(stmt))
            except Exception:
                # Compatibilidad con motores que no soportan IF NOT EXISTS en todos los índices.
                pass

        if db.engine.dialect.name == "postgresql" and "products" in existing_tables:
            # Legacy global uniqueness on barcode must be removed in favor of tenant-scoped uniqueness.
            try:
                connection.execute(text("ALTER TABLE products DROP CONSTRAINT IF EXISTS products_barcode_key"))
            except Exception:
                pass
            try:
                connection.execute(text("DROP INDEX IF EXISTS ix_products_barcode"))
            except Exception:
                pass


@app.cli.command("init-db")
def init_db_command():
    db.create_all()
    ensure_database_schema()
    create_admin_user()
    ensure_primary_superadmin()
    print("Base de datos inicializada correctamente.")


if __name__ == "__main__":
    bootstrap_database()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


bootstrap_database()

