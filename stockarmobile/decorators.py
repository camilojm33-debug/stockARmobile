"""Shared auth/tenant decorators with app-compatible behavior."""

from functools import wraps

from flask import abort, flash, redirect, url_for
from flask_login import current_user, login_required

from .constants import ROLE_ADMIN, ROLE_SELLER, ROLE_SUPERADMIN
from .permissions import user_role
from .responses import api_error
from .tenant import get_current_company_id


def tenant_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        from app import get_company_access_state, is_api_request

        if user_role(current_user) == ROLE_SUPERADMIN:
            if is_api_request():
                return api_error("El panel de empresa no está disponible para SuperAdmin.", 403)
            flash("El panel de empresa no está disponible para SuperAdmin.", "warning")
            return redirect(url_for("saas.index"))

        company_id = get_current_company_id(current_user)
        if company_id is None:
            if is_api_request():
                return api_error("No hay contexto de empresa activo.", 403)
            flash("No hay contexto de empresa activo para esta sesión.", "warning")
            return redirect(url_for("auth.login"))

        state = get_company_access_state(company_id)
        if not state["can_access"]:
            if is_api_request():
                return api_error(state["reason"], 403)
            return redirect(url_for("access_status"))
        return func(*args, **kwargs)

    return decorated


def superadmin_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if user_role(current_user) != ROLE_SUPERADMIN:
            abort(403)
        return func(*args, **kwargs)

    return decorated


def company_admin_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if user_role(current_user) != ROLE_ADMIN:
            abort(403)
        if get_current_company_id(current_user) is None:
            abort(403)
        return func(*args, **kwargs)

    return decorated


def seller_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        from app import ReferralSeller, is_api_request, model_table_exists

        if not model_table_exists(ReferralSeller):
            flash("El programa de referidos todavía no está disponible porque faltan migraciones.", "warning")
            return redirect(url_for("dashboard.index"))

        role = user_role(current_user)
        if role == ROLE_SELLER:
            return func(*args, **kwargs)
        if role == ROLE_SUPERADMIN:
            abort(403)

        profile = ReferralSeller.query.filter_by(user_id=current_user.id, active=True).first()
        if profile is None:
            if is_api_request():
                return api_error("Perfil de referido no activo.", 403)
            flash("Activa tu Programa de Referidos para acceder al portal.", "info")
            return redirect(url_for("referrals.activate_seller"))
        return func(*args, **kwargs)

    return decorated


def trial_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        from app import get_company_access_state

        if user_role(current_user) == ROLE_SUPERADMIN:
            flash("El panel de empresa no está disponible para SuperAdmin.", "warning")
            return redirect(url_for("saas.index"))

        company_id = get_current_company_id(current_user)
        if company_id is None:
            return redirect(url_for("auth.login"))
        state = get_company_access_state(company_id)
        if not state["can_access"]:
            abort(403)
        return func(*args, **kwargs)

    return decorated
