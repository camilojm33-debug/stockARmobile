"""Modulo de compras: proveedores, ordenes e ingreso de mercaderia."""

from datetime import datetime
from io import BytesIO

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import false, or_
from stockarmobile.helpers.numbers import safe_float

bp = Blueprint("purchases", __name__)


def _to_float(value, default=0.0):
    return safe_float(value, default)


def _can_manage_purchases():
    return getattr(current_user, "role", None) in {"admin", "superadmin"}


def _resolve_company_id(required=False):
    from app import Company
    role = getattr(current_user, "role", None)
    if role == "superadmin":
        company_id = request.values.get("company_id", type=int)
    else:
        company_id = getattr(current_user, "company_id", None)
    if required and not company_id:
        return None
    if company_id and Company.query.filter_by(id=company_id).first() is None:
        return None
    return company_id


def _company_scope(query, model, company_id):
    if not hasattr(model, "company_id"):
        return query
    if company_id is None:
        if getattr(current_user, "role", None) != "superadmin":
            return query.filter(false())
        return query
    return query.filter(model.company_id == company_id)


def _require_company_context_or_forbid(company_id):
    if getattr(current_user, "role", None) == "superadmin":
        return
    if company_id is None:
        abort(403)


def _superadmin_companies():
    from app import Company
    return Company.query.filter(Company.active.is_(True)).order_by(Company.name.asc()).all()


def _purchase_access_guard():
    if not _can_manage_purchases():
        abort(403)
    return None


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    from app import Product, PurchaseItem, PurchaseOrder, Supplier, db, record_audit, utcnow
    _purchase_access_guard()
    company_id = _resolve_company_id(required=False)
    _require_company_context_or_forbid(company_id)
    if getattr(current_user, "role", None) == "superadmin" and company_id is None:
        return render_template("compras/index.html", suppliers=[], products=[], purchases=[], selected_company_id=None, companies=_superadmin_companies(), search="", status_filter="active", supplier_stats={}, company_required=True)
    if request.method == "POST":
        if company_id is None:
            flash("Selecciona una empresa para continuar.", "warning")
            return redirect(url_for("purchases.index"))
        product_id = request.form.get("product_id", type=int)
        supplier_id = request.form.get("supplier_id", type=int)
        quantity = _to_float(request.form.get("quantity"))
        unit_cost = _to_float(request.form.get("unit_cost"))
        if not product_id or quantity <= 0 or unit_cost < 0:
            flash("Completa producto, cantidad y costo para registrar la compra.", "danger")
            return redirect(url_for("purchases.index", company_id=company_id))
        product = _company_scope(db.session.query(Product), Product, company_id).filter(Product.id == product_id).first()
        if product is None:
            flash("Producto no encontrado.", "danger")
            return redirect(url_for("purchases.index", company_id=company_id))
        supplier = None
        if supplier_id:
            supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id, Supplier.active.is_(True)).first()
            if supplier is None:
                flash("Proveedor no encontrado para esta empresa.", "danger")
                return redirect(url_for("purchases.index", company_id=company_id))
        previous_stock = float(product.stock or 0)
        previous_cost = float(product.cost_price or 0)
        total_units = previous_stock + quantity
        average_cost = ((previous_stock * previous_cost) + (quantity * unit_cost)) / total_units if total_units else unit_cost
        order = PurchaseOrder(supplier_id=supplier.id if supplier else None, company_id=company_id, date=utcnow(), status="recibida", subtotal=quantity * unit_cost, total_amount=quantity * unit_cost, note=request.form.get("note"))
        db.session.add(order)
        db.session.flush()
        db.session.add(PurchaseItem(purchase_order_id=order.id, product_id=product.id, quantity=quantity, unit_cost=unit_cost))
        product.stock = total_units
        product.cost_price = average_cost
        product.margin = float(product.price or 0) - average_cost
        product.profit_percent = (product.margin / average_cost * 100) if average_cost else 0
        record_audit(action="purchase_create", entity="purchase_order", entity_id=order.id, detail=f"Compra registrada producto={product.id} qty={quantity}")
        db.session.commit()
        flash("Compra registrada y stock actualizado.", "success")
        return redirect(url_for("purchases.index", company_id=company_id))
    suppliers = _company_scope(Supplier.query.filter_by(active=True), Supplier, company_id).order_by(Supplier.name).all()
    products = _company_scope(Product.query.filter_by(active=True), Product, company_id).order_by(Product.name).all()
    purchases = _company_scope(PurchaseOrder.query, PurchaseOrder, company_id).order_by(PurchaseOrder.date.desc()).limit(20).all()
    return render_template("compras/index.html", suppliers=suppliers, products=products, purchases=purchases, selected_company_id=company_id, companies=_superadmin_companies() if getattr(current_user, "role", None) == "superadmin" else [], search="", status_filter="active", supplier_stats={}, company_required=False)


@bp.route("/proveedores", methods=["GET", "POST"])
@login_required
def suppliers_panel():
    from app import PurchaseOrder, Supplier, db, record_audit
    _purchase_access_guard()
    company_id = _resolve_company_id(required=False)
    _require_company_context_or_forbid(company_id)
    if getattr(current_user, "role", None) == "superadmin" and company_id is None:
        return render_template("compras/suppliers.html", suppliers=[], supplier_stats={}, selected_company_id=None, companies=_superadmin_companies(), search="", status_filter="active", company_required=True)
    if request.method == "POST":
        if company_id is None:
            flash("Selecciona una empresa para continuar.", "warning")
            return redirect(url_for("purchases.suppliers_panel"))
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("El proveedor necesita nombre.", "danger")
            return redirect(url_for("purchases.suppliers_panel", company_id=company_id))
        supplier = Supplier(company_id=company_id, name=name, email=(request.form.get("email") or "").strip() or None, phone=(request.form.get("phone") or "").strip() or None, whatsapp=(request.form.get("whatsapp") or "").strip() or None, address=(request.form.get("address") or "").strip() or None, notes=(request.form.get("notes") or "").strip() or None, active=True)
        db.session.add(supplier)
        db.session.flush()
        record_audit(action="supplier_create", entity="supplier", entity_id=supplier.id, detail=f"Proveedor creado: {supplier.name}")
        db.session.commit()
        flash("Proveedor creado.", "success")
        return redirect(url_for("purchases.suppliers_panel", company_id=company_id))
    search = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "active").strip().lower()
    supplier_query = _company_scope(Supplier.query, Supplier, company_id)
    if search:
        like = f"%{search}%"
        supplier_query = supplier_query.filter(or_(Supplier.name.ilike(like), Supplier.email.ilike(like), Supplier.phone.ilike(like), Supplier.whatsapp.ilike(like), Supplier.address.ilike(like), Supplier.notes.ilike(like)))
    if status_filter == "active":
        supplier_query = supplier_query.filter(Supplier.active.is_(True))
    elif status_filter == "inactive":
        supplier_query = supplier_query.filter(Supplier.active.is_(False))
    suppliers = supplier_query.order_by(Supplier.name.asc(), Supplier.id.desc()).all()
    stats_rows = _company_scope(db.session.query(PurchaseOrder.supplier_id, db.func.count(PurchaseOrder.id).label("purchase_count"), db.func.coalesce(db.func.sum(PurchaseOrder.total_amount), 0).label("total_amount")), PurchaseOrder, company_id).filter(PurchaseOrder.supplier_id.isnot(None)).group_by(PurchaseOrder.supplier_id).all()
    supplier_stats = {int(row.supplier_id): {"purchase_count": int(row.purchase_count or 0), "total_amount": float(row.total_amount or 0)} for row in stats_rows if row.supplier_id is not None}
    return render_template("compras/suppliers.html", suppliers=suppliers, supplier_stats=supplier_stats, selected_company_id=company_id, companies=_superadmin_companies() if getattr(current_user, "role", None) == "superadmin" else [], search=search, status_filter=status_filter, company_required=False)


@bp.route("/proveedores/<int:supplier_id>/compras")
@login_required
def supplier_purchases(supplier_id):
    from app import Company, PurchaseOrder, Supplier, db
    _purchase_access_guard()
    company_id = _resolve_company_id(required=True)
    _require_company_context_or_forbid(company_id)
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    query = _company_scope(PurchaseOrder.query, PurchaseOrder, company_id).filter(PurchaseOrder.supplier_id == supplier.id)
    search = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(or_(PurchaseOrder.note.ilike(like), db.cast(PurchaseOrder.id, db.String).ilike(like)))
    if status:
        query = query.filter(PurchaseOrder.status.ilike(status))
    if date_from:
        try: query = query.filter(PurchaseOrder.date >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError: pass
    if date_to:
        try: query = query.filter(PurchaseOrder.date < datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
        except ValueError: pass
    purchases = query.order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc()).all()
    return render_template("compras/supplier_purchases.html", supplier=supplier, purchases=purchases, selected_company_id=company_id, search=search, status=status, date_from=date_from, date_to=date_to)


@bp.route("/proveedores/<int:supplier_id>/compras/export.xlsx")
@login_required
def supplier_purchases_excel(supplier_id):
    from app import PurchaseOrder, Supplier, PurchaseItem, db
    from openpyxl import Workbook
    _purchase_access_guard()
    company_id = _resolve_company_id(required=True)
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    purchases = _filtered_supplier_purchases(PurchaseOrder, supplier, company_id, db)
    wb = Workbook(); ws = wb.active; ws.title = "Compras"
    ws.append(["Compra", "Fecha", "Proveedor", "Producto", "Cantidad", "Costo unitario", "Subtotal", "Total", "Estado", "Observaciones"])
    for purchase in purchases:
        if purchase.items:
            for item in purchase.items:
                ws.append([purchase.id, purchase.date.strftime("%Y-%m-%d %H:%M") if purchase.date else "", supplier.name, item.product.name if item.product else f"Producto {item.product_id}", item.quantity, float(item.unit_cost or 0), float((item.quantity or 0) * (item.unit_cost or 0)), float(purchase.total_amount or 0), purchase.status or "", purchase.note or ""])
        else:
            ws.append([purchase.id, purchase.date.strftime("%Y-%m-%d %H:%M") if purchase.date else "", supplier.name, "", "", "", "", float(purchase.total_amount or 0), purchase.status or "", purchase.note or ""])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 2, 42)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"compras_{supplier.id}.xlsx")


@bp.route("/proveedores/<int:supplier_id>/compras/export.pdf")
@login_required
def supplier_purchases_pdf(supplier_id):
    from app import Supplier, PurchaseOrder, db
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    _purchase_access_guard()
    company_id = _resolve_company_id(required=True)
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    purchases = _filtered_supplier_purchases(PurchaseOrder, supplier, company_id, db)
    buf = BytesIO(); pdf = canvas.Canvas(buf, pagesize=A4); width, height = A4
    pdf.setTitle(f"Compras - {supplier.name}"); y = height - 45
    pdf.setFont("Helvetica-Bold", 15); pdf.drawString(40, y, "StockArmobile - Compras por proveedor"); y -= 22
    pdf.setFont("Helvetica", 10); pdf.drawString(40, y, f"Proveedor: {supplier.name}"); y -= 18
    pdf.drawString(40, y, f"Generado: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"); y -= 24
    pdf.setFont("Helvetica-Bold", 9); pdf.drawString(40, y, "Compra"); pdf.drawString(90, y, "Fecha"); pdf.drawString(185, y, "Productos"); pdf.drawRightString(width - 40, y, "Total"); y -= 15
    pdf.setFont("Helvetica", 8)
    for purchase in purchases:
        if y < 45: pdf.showPage(); y = height - 45; pdf.setFont("Helvetica", 8)
        names = ", ".join((item.product.name if item.product else f"Producto {item.product_id}") for item in purchase.items) or "Sin detalle"
        if len(names) > 65: names = names[:62] + "..."
        pdf.drawString(40, y, f"#{purchase.id}"); pdf.drawString(90, y, purchase.date.strftime("%d/%m/%Y") if purchase.date else ""); pdf.drawString(185, y, names); pdf.drawRightString(width - 40, y, f"${float(purchase.total_amount or 0):,.2f}"); y -= 13
    pdf.save(); buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"compras_{supplier.id}.pdf")


def _filtered_supplier_purchases(PurchaseOrder, supplier, company_id, db):
    query = _company_scope(PurchaseOrder.query, PurchaseOrder, company_id).filter(PurchaseOrder.supplier_id == supplier.id)
    search = (request.args.get("q") or "").strip(); status = (request.args.get("status") or "").strip().lower()
    if search:
        like = f"%{search}%"; query = query.filter(or_(PurchaseOrder.note.ilike(like), db.cast(PurchaseOrder.id, db.String).ilike(like)))
    if status: query = query.filter(PurchaseOrder.status.ilike(status))
    for key, op in (("date_from", ">="), ("date_to", "<")):
        value = (request.args.get(key) or "").strip()
        if value:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
                if key == "date_to": parsed = parsed.replace(hour=23, minute=59, second=59)
                query = query.filter(PurchaseOrder.date >= parsed if op == ">=" else PurchaseOrder.date < parsed)
            except ValueError: pass
    return query.order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc()).all()


@bp.route("/proveedores/<int:supplier_id>/compras/<int:purchase_id>/editar", methods=["GET", "POST"])
@login_required
def edit_supplier_purchase(supplier_id, purchase_id):
    from app import Product, PurchaseItem, PurchaseOrder, Supplier, db, record_audit
    _purchase_access_guard(); company_id = _resolve_company_id(required=True); _require_company_context_or_forbid(company_id)
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    purchase = _company_scope(db.session.query(PurchaseOrder), PurchaseOrder, company_id).filter(PurchaseOrder.id == purchase_id, PurchaseOrder.supplier_id == supplier.id).first_or_404()
    products = _company_scope(Product.query.filter_by(active=True), Product, company_id).order_by(Product.name).all()
    if request.method == "POST":
        product_id = request.form.get("product_id", type=int); quantity = _to_float(request.form.get("quantity")); unit_cost = _to_float(request.form.get("unit_cost")); status = (request.form.get("status") or purchase.status or "recibida").strip(); note = (request.form.get("note") or "").strip() or None
        product = _company_scope(db.session.query(Product), Product, company_id).filter(Product.id == product_id, Product.active.is_(True)).first() if product_id else None
        if product is None or quantity <= 0 or unit_cost < 0:
            flash("Selecciona un producto y valores válidos.", "danger")
            return redirect(url_for("purchases.edit_supplier_purchase", supplier_id=supplier.id, purchase_id=purchase.id, company_id=company_id))
        old_item = purchase.items[0] if purchase.items else None
        if len(purchase.items) > 1:
            flash("Esta compra contiene varias líneas. Su edición múltiple se habilitará en la próxima versión; usa la edición general de Compras.", "warning")
            return redirect(url_for("purchases.index", company_id=company_id))
        old_product = old_item.product if old_item else None
        if old_item and old_product:
            old_product.stock = float(old_product.stock or 0) - float(old_item.quantity or 0)
        product.stock = float(product.stock or 0) + quantity
        purchase.supplier_id = supplier.id; purchase.status = status; purchase.note = note; purchase.subtotal = quantity * unit_cost; purchase.total_amount = quantity * unit_cost
        if old_item:
            old_item.product_id = product.id; old_item.quantity = quantity; old_item.unit_cost = unit_cost
        else:
            db.session.add(PurchaseItem(purchase_order_id=purchase.id, product_id=product.id, quantity=quantity, unit_cost=unit_cost))
        record_audit(action="purchase_update", entity="purchase_order", entity_id=purchase.id, detail=f"Compra actualizada proveedor={supplier.id} producto={product.id} qty={quantity}")
        db.session.commit(); flash("Compra actualizada correctamente.", "success")
        return redirect(url_for("purchases.supplier_purchases", supplier_id=supplier.id, company_id=company_id))
    return render_template("compras/edit_purchase.html", supplier=supplier, purchase=purchase, products=products, selected_company_id=company_id)


@bp.route("/proveedores/<int:supplier_id>/compras/import.xlsx", methods=["POST"])
@login_required
def import_supplier_purchases(supplier_id):
    from app import Product, PurchaseItem, PurchaseOrder, Supplier, db, record_audit, utcnow
    from openpyxl import load_workbook
    _purchase_access_guard(); company_id = _resolve_company_id(required=True); _require_company_context_or_forbid(company_id)
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Selecciona un archivo Excel .xlsx válido.", "danger"); return redirect(url_for("purchases.supplier_purchases", supplier_id=supplier.id, company_id=company_id))
    try:
        wb = load_workbook(upload, read_only=True, data_only=True); ws = wb.active; rows = list(ws.iter_rows(values_only=True)); wb.close()
        if not rows: raise ValueError("El Excel está vacío.")
        headers = {str(v or "").strip().lower(): i for i, v in enumerate(rows[0])}
        def col(*names):
            for name in names:
                if name in headers: return headers[name]
            return None
        pcol = col("producto", "product", "codigo", "código", "id producto")
        qcol = col("cantidad", "quantity")
        ccol = col("costo unitario", "unit_cost", "costo", "precio costo")
        if qcol is None or ccol is None or pcol is None: raise ValueError("Faltan columnas obligatorias: Producto, Cantidad y Costo unitario.")
        imported = 0
        for row in rows[1:]:
            if not row or all(v in (None, "") for v in row): continue
            raw_product = row[pcol]; quantity = _to_float(row[qcol]); unit_cost = _to_float(row[ccol])
            product = None
            if str(raw_product).strip().isdigit(): product = _company_scope(db.session.query(Product), Product, company_id).filter(Product.id == int(raw_product), Product.active.is_(True)).first()
            if product is None: product = _company_scope(Product.query, Product, company_id).filter(Product.name.ilike(str(raw_product).strip()), Product.active.is_(True)).first()
            if product is None or quantity <= 0 or unit_cost < 0: continue
            order = PurchaseOrder(supplier_id=supplier.id, company_id=company_id, date=utcnow(), status="recibida", subtotal=quantity * unit_cost, total_amount=quantity * unit_cost, note="Importada desde Excel")
            db.session.add(order); db.session.flush(); db.session.add(PurchaseItem(purchase_order_id=order.id, product_id=product.id, quantity=quantity, unit_cost=unit_cost)); product.stock = float(product.stock or 0) + quantity; product.cost_price = unit_cost; imported += 1
        if not imported: raise ValueError("No se encontraron filas válidas para importar.")
        record_audit(action="purchase_import_excel", entity="supplier", entity_id=supplier.id, detail=f"Compras importadas={imported}"); db.session.commit(); flash(f"Se importaron {imported} compras.", "success")
    except Exception as exc:
        db.session.rollback(); flash(f"No se pudo importar el Excel: {exc}", "danger")
    return redirect(url_for("purchases.supplier_purchases", supplier_id=supplier.id, company_id=company_id))


@bp.route("/proveedores/<int:supplier_id>/compras/plantilla.xlsx")
@login_required
def supplier_purchase_template(supplier_id):
    from app import Supplier, db
    from openpyxl import Workbook
    _purchase_access_guard(); company_id = _resolve_company_id(required=True)
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    wb = Workbook(); ws = wb.active; ws.title = "Compras"; ws.append(["Producto", "Cantidad", "Costo unitario"]); ws.append(["Ejemplo", 1, 1000])
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"plantilla_compras_{supplier.id}.xlsx")


@bp.route("/proveedores/<int:supplier_id>/update", methods=["POST"])
@login_required
def update_supplier(supplier_id):
    from app import Supplier, db, record_audit
    _purchase_access_guard(); company_id = _resolve_company_id(required=True)
    if company_id is None: flash("Empresa inválida.", "danger"); return redirect(url_for("purchases.suppliers_panel"))
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    name = (request.form.get("name") or "").strip()
    if not name: flash("El proveedor necesita nombre.", "danger"); return redirect(url_for("purchases.suppliers_panel", company_id=company_id))
    supplier.name = name; supplier.email = (request.form.get("email") or "").strip() or None; supplier.phone = (request.form.get("phone") or "").strip() or None; supplier.whatsapp = (request.form.get("whatsapp") or "").strip() or None; supplier.address = (request.form.get("address") or "").strip() or None; supplier.notes = (request.form.get("notes") or "").strip() or None
    db.session.add(supplier); record_audit(action="supplier_update", entity="supplier", entity_id=supplier.id, detail=f"Proveedor actualizado: {supplier.name}"); db.session.commit(); flash("Proveedor actualizado.", "success")
    return redirect(url_for("purchases.suppliers_panel", company_id=company_id))


@bp.route("/proveedores/<int:supplier_id>/toggle", methods=["POST"])
@login_required
def toggle_supplier(supplier_id):
    from app import Supplier, db, record_audit
    _purchase_access_guard(); company_id = _resolve_company_id(required=True)
    if company_id is None: flash("Empresa inválida.", "danger"); return redirect(url_for("purchases.suppliers_panel"))
    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    supplier.active = not bool(supplier.active); db.session.add(supplier); action = "supplier_activate" if supplier.active else "supplier_deactivate"; detail = "Proveedor reactivado" if supplier.active else "Proveedor desactivado"
    record_audit(action=action, entity="supplier", entity_id=supplier.id, detail=f"{detail}: {supplier.name}"); db.session.commit(); flash("Proveedor actualizado.", "success")
    return redirect(url_for("purchases.suppliers_panel", company_id=company_id))
