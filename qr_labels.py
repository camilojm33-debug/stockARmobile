"""QR, EAN13, etiquetas PDF y tickets PDF."""
from io import BytesIO

import barcode
import qrcode
from barcode.writer import ImageWriter
from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import SimpleDocTemplate, Spacer
from sqlalchemy.orm import selectinload
from app import tenant_required, utcnow

bp = Blueprint("qr_labels", __name__)


def _font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _ean_payload(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        digits = "0"
    return digits[:12].zfill(12)


def generate_ean13_code(barcode_string):
    """Genera una imagen PIL con codigo EAN13."""
    ean = barcode.get("ean13", _ean_payload(barcode_string), writer=ImageWriter())
    buffer = BytesIO()
    ean.write(buffer, options={"write_text": True})
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def generate_code128_code(barcode_string):
    code128 = barcode.get("code128", str(barcode_string or "SIN-CODIGO"), writer=ImageWriter())
    buffer = BytesIO()
    code128.write(buffer, options={"write_text": True})
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def generate_qr_code(text):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(str(text))
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def create_product_label(barcode_string, product_name, price, size=(300, 400)):
    img = Image.new("RGB", size, color="white")
    draw = ImageDraw.Draw(img)
    font_large = _font(32)
    font_medium = _font(18)
    font_small = _font(12)

    name_text = str(product_name or "Producto")[:32]
    price_text = f"ARS {float(price or 0):.2f}"
    draw.text((size[0] / 2, 35), name_text, fill="black", anchor="mm", font=font_medium)
    draw.text((size[0] / 2, 80), price_text, fill="black", anchor="mm", font=font_large)

    qr_img = generate_qr_code(barcode_string or name_text)
    qr_img.thumbnail((105, 105), Image.Resampling.LANCZOS)
    img.paste(qr_img, (size[0] - 125, 115))

    barcode_img = generate_code128_code(barcode_string) if not str(barcode_string or "").isdigit() else generate_ean13_code(barcode_string)
    barcode_img.thumbnail((220, 95), Image.Resampling.LANCZOS)
    img.paste(barcode_img, ((size[0] - barcode_img.width) // 2, size[1] - 125))
    draw.text((size[0] / 2, size[1] - 18), str(barcode_string or "")[:32], fill="black", anchor="mm", font=font_small)
    return img


def _label_dimensions_mm(size_key):
    return {
        "30x20": (30, 20),
        "40x30": (40, 30),
        "50x25": (50, 25),
        "60x40": (60, 40),
        "80x50": (80, 50),
    }.get(size_key, (50, 25))


def _payment_qr_text():
    alias = request.form.get("payment_alias") or request.args.get("alias") or ""
    cbu = request.form.get("payment_cbu") or request.args.get("cbu") or ""
    cvu = request.form.get("payment_cvu") or request.args.get("cvu") or ""
    text = request.form.get("payment_text") or request.args.get("text") or ""
    url = request.form.get("payment_url") or request.args.get("url") or ""
    return "\n".join(part for part in [text, f"Alias: {alias}" if alias else "", f"CBU: {cbu}" if cbu else "", f"CVU: {cvu}" if cvu else "", url] if part)


@bp.route("/")
@tenant_required
def generate():
    from app import Product, scope_query_to_company

    search = request.args.get("search", "")
    query = scope_query_to_company(Product.query.filter_by(active=True), Product)
    if search:
        like = f"%{search}%"
        query = query.filter((Product.name.ilike(like)) | (Product.barcode.ilike(like)))
    products = query.order_by(Product.name).all()
    return render_template("qr_labels/generate.html", products=products)


@bp.route("/image/<int:id>")
@tenant_required
def qr_image(id):
    from app import Product, scope_query_to_company

    product = scope_query_to_company(Product.query, Product).filter(Product.id == id).first_or_404()
    img = generate_qr_code(product.barcode or product.id)
    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", download_name=f"qr_{product.id}.png")


@bp.route("/code128/<int:id>")
@tenant_required
def code128_image(id):
    from app import Product, scope_query_to_company

    product = scope_query_to_company(Product.query, Product).filter(Product.id == id).first_or_404()
    img = generate_code128_code(product.barcode or product.id)
    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", download_name=f"code128_{product.id}.png")


@bp.route("/payment-qr.png")
@tenant_required
def payment_qr():
    img = generate_qr_code(_payment_qr_text() or "StockArmobile")
    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", download_name="qr_cobro.png")


@bp.route("/label/<int:id>")
@tenant_required
def print_single(id):
    from app import Product, scope_query_to_company

    product = scope_query_to_company(Product.query, Product).filter(Product.id == id).first_or_404()
    img = create_product_label(product.barcode, product.name, product.price)
    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", as_attachment=True, download_name=f"etiqueta_{product.id}.png")


@bp.route("/label-sheet/<int:id>", methods=["POST"])
@tenant_required
def print_product_sheet(id):
    from app import Product, scope_query_to_company

    product = scope_query_to_company(Product.query, Product).filter(Product.id == id).first_or_404()
    size_key = request.form.get("label_size") or "50x25"
    quantity = request.form.get("quantity", 1, type=int) or 1
    width_mm, height_mm = _label_dimensions_mm(size_key)
    include_name = bool(request.form.get("include_name"))
    include_price = bool(request.form.get("include_price"))
    include_code = bool(request.form.get("include_code"))
    include_qr = bool(request.form.get("include_qr"))
    include_ean = bool(request.form.get("include_ean"))
    include_code128 = bool(request.form.get("include_code128"))
    include_date = bool(request.form.get("include_date"))

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm, bottomMargin=8 * mm)
    elements = []
    for _ in range(max(quantity, 1)):
        img = Image.new("RGB", (int(width_mm * 8), int(height_mm * 8)), "white")
        draw = ImageDraw.Draw(img)
        y = 8
        if include_name:
            draw.text((10, y), product.name[:28], fill="black", font=_font(14))
            y += 20
        if include_price:
            draw.text((10, y), f"${float(product.price or 0):.2f}", fill="black", font=_font(18))
            y += 24
        if include_code:
            draw.text((10, y), str(product.barcode or product.id)[:28], fill="black", font=_font(10))
            y += 14
        if include_qr:
            qr_img = generate_qr_code(product.barcode or product.id)
            qr_img.thumbnail((52, 52), Image.Resampling.LANCZOS)
            img.paste(qr_img, (img.width - 60, 8))
        if include_ean or include_code128:
            code_img = generate_code128_code(product.barcode or product.id) if include_code128 else generate_ean13_code(product.barcode)
            code_img.thumbnail((img.width - 18, 48), Image.Resampling.LANCZOS)
            img.paste(code_img, (9, img.height - 56))
        if include_date:
            draw.text((10, img.height - 12), utcnow().strftime("%Y-%m-%d"), fill="black", font=_font(9))
        img_buffer = BytesIO()
        img.save(img_buffer, "PNG")
        img_buffer.seek(0)
        elements.append(PdfImage(img_buffer, width=width_mm * mm, height=height_mm * mm))
        elements.append(Spacer(1, 3 * mm))
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=f"etiquetas_{product.id}_{size_key}.pdf")


@bp.route("/labels", methods=["GET"])
@tenant_required
def generate_labels():
    codigo = request.args.get("codigo")
    if codigo:
        from app import Product, scope_query_to_company

        product = scope_query_to_company(Product.query.filter_by(barcode=codigo), Product).first()
        if product:
            return redirect(url_for("qr_labels.print_single", id=product.id))
    return redirect(url_for("qr_labels.generate"))


@bp.route("/print-all", methods=["POST"])
@tenant_required
def print_all():
    from app import Product, scope_query_to_company

    products = scope_query_to_company(Product.query.filter_by(active=True), Product).order_by(Product.name).all()
    if not products:
        flash("No hay productos para imprimir.", "warning")
        return redirect(url_for("qr_labels.generate"))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    label_size = request.form.get("size") or request.args.get("size") or "standard"
    copies = request.form.get("copies", request.args.get("copies", 1), type=int) or 1
    dimensions = {"small": (260, 320), "standard": (300, 400), "large": (380, 480)}.get(label_size, (300, 400))
    for product in products:
        img = create_product_label(product.barcode, product.name, product.price, size=dimensions)
        img_buffer = BytesIO()
        img.save(img_buffer, "PNG")
        img_buffer.seek(0)
        for _ in range(max(copies, 1)):
            elements.append(PdfImage(img_buffer, width=180, height=240))
            elements.append(Spacer(1, 12))
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="etiquetas_productos.pdf")


@bp.route("/ticket/<int:sale_id>.pdf")
@tenant_required
def generate_pdf_ticket(sale_id):
    from app import Sale, SaleItem, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    ticket_img = Image.new("RGB", (420, 560), "white")
    draw = ImageDraw.Draw(ticket_img)
    y = 20
    for line in _ticket_lines(sale):
        draw.text((20, y), line, fill="black", font=_font(16))
        y += 26
    img_buffer = BytesIO()
    ticket_img.save(img_buffer, "PNG")
    img_buffer.seek(0)
    elements.append(PdfImage(img_buffer, width=300, height=400))
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=False, download_name=f"ticket_{sale.id}.pdf")


def _ticket_lines(sale):
    lines = ["STOCK ARMOBILE - TICKET", f"Venta #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}", "-" * 28]
    for item in sale.items:
        name = item.product.name if item.product else "Producto"
        lines.append(f"{name[:20]} x{item.quantity} ${item.total_amount:.2f}")
    lines += ["-" * 28, f"Subtotal: ${sale.subtotal:.2f}", f"IVA: ${sale.tax:.2f}", f"Total: ${sale.total_amount:.2f}"]
    return lines
