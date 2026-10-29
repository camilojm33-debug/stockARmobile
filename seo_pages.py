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

_COMMERCIAL_TOOL_PAGES = {
    "mayoristas": {
        "slug": "precios-mayoristas-para-comercios",
        "title": "Software de precios mayoristas para comercios | StockArmobile",
        "description": "Gestioná listas de precios mayoristas, descuentos por cantidad y tarifas por producto o cliente con StockArmobile.",
        "icon": "bi-people",
        "eyebrow": "Ventas por volumen",
        "heading": "Precios mayoristas para vender por volumen sin perder el control",
        "intro": "Definí condiciones comerciales para tus clientes mayoristas sin reemplazar el precio minorista de tu catálogo. Las listas y reglas vigentes permiten resolver el precio según el cliente, el producto y la cantidad.",
        "benefits_heading": "Organizá tus precios por cliente y cantidad",
        "sections": [
            {"title": "Listas de precios por comercio", "body": "Creá listas con nombre, código y período de vigencia. Asigná la lista correspondiente a cada cliente mayorista para que el sistema aplique las condiciones adecuadas."},
            {"title": "Descuentos generales y tarifas por producto", "body": "Configurá reglas generales por cantidad para cubrir una lista completa o definí tarifas específicas cuando un producto necesite otra condición. El sistema elige el tramo aplicable sin acumular porcentajes."},
            {"title": "Carga masiva con Excel", "body": "Importá múltiples tarifas desde una plantilla, validá los datos y conservá los tramos que no forman parte de la importación."},
        ],
        "bullets": ["Vigencias por lista", "Descuentos por cantidad", "Precios fijos o porcentuales", "Asignación de listas desde la ficha del cliente"],
        "faq": [
            {"question": "¿Los precios minoristas se reemplazan?", "answer": "No. Las listas mayoristas no modifican el precio minorista base del producto."},
            {"question": "¿Se acumulan las promociones minoristas?", "answer": "No automáticamente. En la versión actual, la tarifa mayorista es final y no se apilan promociones minoristas sobre ella."},
        ],
    },
    "promociones": {
        "slug": "sistema-de-promociones-comerciales",
        "title": "Sistema de promociones comerciales para comercios | StockArmobile",
        "description": "Configurá promociones 2x1, 3x2 y descuentos por cantidad sobre productos o categorías con StockArmobile.",
        "icon": "bi-tags",
        "eyebrow": "Promociones comerciales",
        "heading": "Creá promociones claras para impulsar las ventas de tu comercio",
        "intro": "Definí reglas de promoción para productos o categorías, elegí cuándo se aplican y dejá que el sistema calcule el descuento correspondiente cuando se cumplen las condiciones.",
        "benefits_heading": "Reglas y vigencias para cada oferta",
        "sections": [
            {"title": "Promociones por unidades", "body": "Configurá reglas del tipo llevás X, pagás Y —como 2x1, 3x2 o 4x2— para aplicar unidades bonificadas cuando la cantidad del producto cumple la condición."},
            {"title": "Descuentos por cantidad", "body": "Aplicá un porcentaje o un importe de descuento a partir de un mínimo de unidades. Podés dirigir la regla a un producto específico o a una categoría."},
            {"title": "Control de vigencia y prioridad", "body": "Definí fechas de inicio y finalización, activá o desactivá reglas y asigná prioridad. El sistema aplica como máximo una promoción por producto y línea."},
        ],
        "bullets": ["2x1, 3x2 y otras combinaciones", "Porcentaje a partir de cierta cantidad", "Descuento por importe y bloque de unidades", "Vigencia y prioridad configurables"],
        "faq": [
            {"question": "¿Puedo aplicar una promoción a toda una categoría?", "answer": "Sí. La regla puede dirigirse a una categoría completa o a un producto específico."},
            {"question": "¿Se suman varias promociones entre sí?", "answer": "No. El motor selecciona como máximo una promoción por producto y línea, conforme a las reglas de prioridad."},
        ],
    },
    "precios": {
        "slug": "control-global-de-precios",
        "title": "Control global de precios para comercios | StockArmobile",
        "description": "Actualizá precios de muchos productos con vista previa, filtros, límites de margen e historial en StockArmobile.",
        "icon": "bi-sliders",
        "eyebrow": "Gestión de precios",
        "heading": "Actualizá muchos precios de una vez, con controles antes de confirmar",
        "intro": "Prepará ajustes masivos en tu catálogo sin editar producto por producto. Revisá cómo cambiarían los precios, verificá límites y márgenes y confirmá solo cuando el resultado sea correcto.",
        "benefits_heading": "Ajustes masivos con revisión y trazabilidad",
        "sections": [
            {"title": "Elegí cómo actualizar", "body": "Aplicá aumentos o disminuciones porcentuales, sumá o restá un importe fijo o establecé un precio específico. Podés configurar reglas de redondeo."},
            {"title": "Seleccioná los productos", "body": "Filtrá por categoría, marca, proveedor o productos con stock para trabajar sobre el grupo correcto."},
            {"title": "Protegé márgenes y revisá resultados", "body": "Definí precios mínimos y máximos, márgenes mínimos y un cambio porcentual máximo. La vista previa separa los cambios bloqueados y no modifica el catálogo hasta que confirmes."},
            {"title": "Historial y reversión protegida", "body": "Los cambios quedan asociados a un lote e historial. La reversión comprueba que el precio y costo no hayan sido modificados después, para evitar sobrescribir cambios posteriores."},
        ],
        "bullets": ["Cambios por porcentaje, importe o precio fijo", "Filtros por categoría, marca, proveedor y stock", "Precios mínimos y máximos", "Control de margen y cambio máximo", "Vista previa antes de aplicar"],
        "faq": [
            {"question": "¿Los precios cambian al crear la vista previa?", "answer": "No. La vista previa calcula el resultado, pero no modifica los productos hasta que confirmás la operación."},
            {"question": "¿Puedo revertir un ajuste?", "answer": "Sí, si el lote reúne las condiciones de reversión. El sistema detecta conflictos cuando los precios o costos fueron modificados después."},
        ],
    },
}


def _render_commercial_tool_page(key: str):
    feature = _COMMERCIAL_TOOL_PAGES[key]
    seo = _seo_context(slug=feature["slug"], title=feature["title"], description=feature["description"])
    route_by_key = {
        "mayoristas": "seo_pages.precios_mayoristas",
        "promociones": "seo_pages.promociones_comerciales",
        "precios": "seo_pages.control_global_de_precios",
    }
    related_pages = [
        {"title": _COMMERCIAL_TOOL_PAGES[other]["heading"], "url": url_for(endpoint)}
        for other, endpoint in route_by_key.items() if other != key
    ]
    return render_template("seo/herramienta-comercial.html", seo=seo, feature=feature, related_pages=related_pages)


@bp.route("/precios-mayoristas-para-comercios")
def precios_mayoristas():
    return _render_commercial_tool_page("mayoristas")


@bp.route("/sistema-de-promociones-comerciales")
def promociones_comerciales():
    return _render_commercial_tool_page("promociones")


@bp.route("/control-global-de-precios")
def control_global_de_precios():
    return _render_commercial_tool_page("precios")
