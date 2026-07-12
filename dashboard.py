"""Blueprint de dashboard: metricas y resumen."""

from flask import Blueprint, render_template
from flask_login import login_required
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
