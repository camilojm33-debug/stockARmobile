"""Paginas publicas SEO/comerciales por tipo de comercio. Sin autenticacion."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template, url_for

bp = Blueprint("seo_pages", __name__)


def _seo_context(*, slug: str, title: str, description: str) -> dict:
    app_base_url = current_app.config["APP_URL"].rstrip("/")
    return {
        "title": title,
        "description": description,
        "url": f"{app_base_url}/{slug}",
        "image": f"{app_base_url}{url_for('static', filename='images/branding/logo.png')}",
        "site_name": "StockArmobile",
    }


@bp.route("/software-para-ferreterias")
def ferreterias():
    seo = _seo_context(
        slug="software-para-ferreterias",
        title="Software para ferreterías en Argentina | StockArmobile",
        description=(
            "Sistema de gestión para ferreterías: control de stock, códigos de barras, ventas, "
            "caja y presupuestos para tus clientes en una sola plataforma."
        ),
    )
    return render_template("seo/software-ferreterias.html", seo=seo)


@bp.route("/software-para-corralones")
def corralones():
    seo = _seo_context(
        slug="software-para-corralones",
        title="Software para corralones en Argentina | StockArmobile",
        description=(
            "Sistema de gestión para corralones: stock de materiales, presupuestos, ventas, "
            "caja y control de proveedores en una sola plataforma."
        ),
    )
    return render_template("seo/software-corralones.html", seo=seo)


@bp.route("/sistema-para-kioscos")
def kioscos():
    seo = _seo_context(
        slug="sistema-para-kioscos",
        title="Sistema para kioscos en Argentina | StockArmobile",
        description=(
            "Sistema de gestión para kioscos: ventas rápidas, control de stock, códigos de barras, "
            "caja y cobros con Mercado Pago en una sola plataforma."
        ),
    )
    return render_template("seo/sistema-kioscos.html", seo=seo)


@bp.route("/sistema-para-supermercados")
def supermercados():
    seo = _seo_context(
        slug="sistema-para-supermercados",
        title="Sistema para supermercados en Argentina | StockArmobile",
        description=(
            "Sistema de gestión para supermercados: control de stock, códigos de barras, ventas, "
            "caja, compras a proveedores y reportes en una sola plataforma."
        ),
    )
    return render_template("seo/sistema-supermercados.html", seo=seo)


@bp.route("/sistema-de-presupuestos")
def presupuestos():
    seo = _seo_context(
        slug="sistema-de-presupuestos",
        title="Sistema de presupuestos para comercios en Argentina | StockArmobile",
        description=(
            "Creá presupuestos para tus clientes, generá PDF, compartilos por WhatsApp o email "
            "y convertí presupuestos en ventas con StockArmobile."
        ),
    )
    return render_template("seo/sistema-de-presupuestos.html", seo=seo)


@bp.route("/control-de-stock")
def control_de_stock():
    seo = _seo_context(
        slug="control-de-stock",
        title="Sistema de control de stock para comercios en Argentina | StockArmobile",
        description=(
            "Controlá el stock de tu comercio, configurá mínimos, detectá productos con bajo "
            "stock y consultá reportes para tomar mejores decisiones."
        ),
    )
    return render_template("seo/control-de-stock.html", seo=seo)
