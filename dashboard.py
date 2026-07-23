"""Blueprint de dashboard: metricas, onboarding y tour guiado."""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from services.dashboard_service import build_dashboard_context
from app import tenant_required

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@tenant_required
def index():
    return render_template("dashboard/index.html", **build_dashboard_context())


@bp.route("/stats")
@tenant_required
def stats():
    from app import Product, Sale, SaleItem, db, scope_query_to_company

    categories_result = (
        scope_query_to_company(
            db.session.query(Product.category.label("category"), db.func.sum(SaleItem.quantity).label("total_sold"))
            .join(Product, SaleItem.product_id == Product.id)
            .join(Sale, SaleItem.sale_id == Sale.id)
            .group_by(Product.category),
            Product,
        ).all()
    )
    categories_list = [cat[0] or "N/A" for cat in categories_result]
    categories_data = [cat[1] or 0 for cat in categories_result]
    return render_template("dashboard/stats.html", categories=categories_list, categories_data=categories_data)


@bp.route("/inicio-rapido")
@tenant_required
def quick_start():
    return render_template("dashboard/quick_start.html")


@bp.route("/onboarding", methods=["GET", "POST"])
@tenant_required
def onboarding():
    pending_company_id = session.get("post_register_onboarding_company_id")
    current_company_id = getattr(current_user, "company_id", None)
    if pending_company_id and int(pending_company_id) != int(current_company_id or 0):
        return redirect(url_for("dashboard.index"))

    steps = [
        {"step": 1, "title": "Datos del negocio", "description": "Completa información comercial, fiscal y de contacto en Mi Empresa."},
        {"step": 2, "title": "Moneda y configuración", "description": "Confirma moneda, formato y ajustes generales antes de operar."},
        {"step": 3, "title": "Productos", "description": "Carga tus productos y define stock mínimo para empezar ordenado."},
        {"step": 4, "title": "Clientes", "description": "Registra clientes frecuentes para ventas rápidas y seguimiento."},
        {"step": 5, "title": "Primera venta", "description": "Abre caja, agrega productos y registra tu primera operación."},
        {"step": 6, "title": "Caja y reporte", "description": "Cierra caja, revisa totales y valida diferencias al final del día."},
    ]

    if request.method == "POST":
        session.pop("post_register_onboarding_company_id", None)
        session["guided_tour_pending"] = True
        session.pop("guided_tour_seen", None)
        flash("Onboarding completado. Te mostramos el recorrido guiado.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("dashboard/onboarding.html", steps=steps, progress=100)


@bp.route("/tour/complete", methods=["POST"])
@tenant_required
def tour_complete():
    session.pop("guided_tour_pending", None)
    session["guided_tour_seen"] = True
    flash("Recorrido guiado finalizado.", "info")
    next_url = (request.form.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("dashboard.index"))
