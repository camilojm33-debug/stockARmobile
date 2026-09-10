"""Reportes CSV, Excel y PDF para ventas, compras, gastos y balance."""

import csv
from datetime import date, timedelta
from io import BytesIO, StringIO

from flask import Blueprint, make_response, render_template, request, send_file
from flask_login import current_user
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from app import tenant_required
from stockarmobile.helpers.dates import local_day_bounds_utc_naive, local_today, format_local_datetime, parse_date_yyyy_mm_dd

bp = Blueprint("reports", __name__)


def _company_timezone_name():
    from app import Company
    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    return (getattr(company, "timezone", None) or "America/Argentina/Buenos_Aires").strip() or "America/Argentina/Buenos_Aires"


def _parse_requested_day(value, fallback=None):
    parsed = parse_date_yyyy_mm_dd(value)
    if parsed is not None:
        return parsed.date()
    return fallback


def _local_date_range(start_value=None, end_value=None):
    tz_name = _company_timezone_name()
    today = local_today(tz_name)
    start_day = _parse_requested_day(start_value, today if not start_value else None)
    end_day = _parse_requested_day(end_value, today if not end_value else None)
    if start_day is None and end_day is None:
        start_day = end_day = today
    elif start_day is None:
        start_day = end_day
    elif end_day is None:
        end_day = start_day
    start_utc, _ = local_day_bounds_utc_naive(start_day, tz_name)
    _, end_utc = local_day_bounds_utc_naive(end_day, tz_name)
    return start_utc, end_utc, start_day, end_day, tz_name


@bp.route("/")
@tenant_required
def index():
    from app import Expense, PurchaseOrder, Sale, db, scope_query_to_company
    start_utc, end_utc, start_day, end_day, tz_name = _local_date_range(request.args.get("desde"), request.args.get("hasta"))

    def _apply_date(q, col):
        return q.filter(col >= start_utc, col < end_utc)

    sales_total = _apply_date(scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)), Sale), Sale.date).scalar() or 0
    purchases_total = _apply_date(scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(PurchaseOrder.total_amount), 0)), PurchaseOrder), PurchaseOrder.date).scalar() or 0
    expenses_total = _apply_date(scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0)), Expense), Expense.date).scalar() or 0
    return render_template(
        "reportes/index.html",
        sales_total=sales_total,
        purchases_total=purchases_total,
        expenses_total=expenses_total,
        desde=start_day.isoformat(),
        hasta=end_day.isoformat(),
        report_timezone=tz_name,
    )


def _balance_rows(start_value=None, end_value=None):
    from app import Expense, PurchaseOrder, Sale, db, scope_query_to_company
    start_utc, end_utc, _, _, _ = _local_date_range(start_value, end_value)

    def total(model, column):
        query = scope_query_to_company(
            db.session.query(db.func.coalesce(db.func.sum(column), 0)), model
        ).filter(column >= start_utc, column < end_utc)
        return float(query.scalar() or 0)

    sales_total = total(Sale, Sale.total_amount)
    purchases_total = total(PurchaseOrder, PurchaseOrder.total_amount)
    expenses_total = total(Expense, Expense.amount)
    return [("Ventas", sales_total), ("Compras", purchases_total), ("Gastos", expenses_total), ("Resultado", sales_total - purchases_total - expenses_total)]


@bp.route("/balance.csv")
@tenant_required
def balance_csv():
    start = request.args.get("desde")
    end = request.args.get("hasta")
    rows = _balance_rows(start, end)
    _, _, start_day, end_day, tz_name = _local_date_range(start, end)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Concepto", "Importe"])
    writer.writerows(rows)
    writer.writerow([])
    writer.writerow(["Desde", start_day.isoformat()])
    writer.writerow(["Hasta", end_day.isoformat()])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="balance_{start_day:%Y%m%d}_{end_day:%Y%m%d}.csv"'
    return response


@bp.route("/<kind>.csv")
@tenant_required
def export_csv(kind):
    rows, filename = _rows_for(kind)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerows(rows)
    _, _, start_day, end_day, _ = _local_date_range(request.args.get("desde"), request.args.get("hasta"))
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}_{start_day:%Y%m%d}_{end_day:%Y%m%d}.csv"'
    return response


@bp.route("/<kind>.xlsx")
@tenant_required
def export_excel(kind):
    rows, filename = _rows_for(kind)
    _, _, start_day, end_day, _ = _local_date_range(request.args.get("desde"), request.args.get("hasta"))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = filename[:31]
    header_fill = PatternFill("solid", fgColor="2563EB")
    header_font = Font(color="FFFFFF", bold=True)
    for row_index, row in enumerate(rows, start=1):
        sheet.append(row)
        if row_index == 1:
            for cell in sheet[row_index]:
                cell.fill = header_fill
                cell.font = header_font
    for column in sheet.columns:
        width = max(len(str(cell.value or "")) for cell in column) + 2
        sheet.column_dimensions[column[0].column_letter].width = min(width, 42)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"{filename}_{start_day:%Y%m%d}_{end_day:%Y%m%d}.xlsx",
    )


@bp.route("/<kind>.pdf")
@tenant_required
def export_pdf(kind):
    rows, filename = _rows_for(kind)
    _, _, start_day, end_day, _ = _local_date_range(request.args.get("desde"), request.args.get("hasta"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    pdf.setTitle(f"StockArmobile - {filename}")
    y = height - 42
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(40, y, f"StockArmobile - Reporte de {filename.title()}")
    y -= 18
    pdf.setFont("Helvetica", 9)
    pdf.drawString(40, y, f"Período: {start_day:%d/%m/%Y} al {end_day:%d/%m/%Y}")
    y -= 24
    headers = rows[0] if rows else ["Sin datos"]
    pdf.setFont("Helvetica-Bold", 7.5)
    x_positions = _pdf_column_positions(headers, width)
    for index, header in enumerate(headers):
        pdf.drawString(x_positions[index], y, _pdf_text(header, 20))
    y -= 13
    pdf.setFont("Helvetica", 7)
    for row in rows[1:]:
        if y < 42:
            pdf.showPage()
            y = height - 42
            pdf.setFont("Helvetica-Bold", 7.5)
            for index, header in enumerate(headers):
                pdf.drawString(x_positions[index], y, _pdf_text(header, 20))
            y -= 13
            pdf.setFont("Helvetica", 7)
        for index, value in enumerate(row):
            pdf.drawString(x_positions[index], y, _pdf_text(value, 20))
        y -= 11
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=f"{filename}_{start_day:%Y%m%d}_{end_day:%Y%m%d}.pdf")


def _pdf_column_positions(headers, width):
    count = max(1, len(headers))
    usable = width - 80
    step = usable / count
    return [40 + step * index for index in range(count)]


def _pdf_text(value, limit=20):
    text = str(value or "").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


@bp.route("/balance.pdf")
@tenant_required
def balance_pdf():
    start = request.args.get("desde")
    end = request.args.get("hasta")
    rows = _balance_rows(start, end)
    _, _, start_day, end_day, _ = _local_date_range(start, end)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setTitle("Balance StockArmobile")
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(72, 740, "StockArmobile - Balance")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(72, 720, f"Período: {start_day:%d/%m/%Y} al {end_day:%d/%m/%Y}")
    y = 688
    for label, amount in rows:
        pdf.drawString(72, y, f"{label}: ${amount:.2f}")
        y -= 22
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=f"balance_{start_day:%Y%m%d}_{end_day:%Y%m%d}.pdf")


@bp.route("/balance.xlsx")
@tenant_required
def balance_excel():
    start = request.args.get("desde")
    end = request.args.get("hasta")
    rows = [["Concepto", "Importe"], *_balance_rows(start, end)]
    _, _, start_day, end_day, _ = _local_date_range(start, end)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Balance"
    for row in rows:
        sheet.append(row)
    header_fill = PatternFill("solid", fgColor="2563EB")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
    sheet.column_dimensions["A"].width = 22
    sheet.column_dimensions["B"].width = 18
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"balance_{start_day:%Y%m%d}_{end_day:%Y%m%d}.xlsx")


def _rows_for(kind):
    from app import CashMovement, Client, Expense, Product, PurchaseOrder, Sale, SaleItem, scope_query_to_company
    from sqlalchemy.orm import selectinload
    start_utc, end_utc, _, _, tz_name = _local_date_range(request.args.get("desde"), request.args.get("hasta"))
    if kind == "ventas":
        query = _date_filter(scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)).order_by(Sale.date.desc()), Sale), Sale.date, start_utc, end_utc)
        return [["ID", "Cliente", "Total", "Fecha", "Unidades"]] + [[s.id, s.customer or "", s.total_amount or 0, format_local_datetime(s.date, tz_name), "; ".join(f"{item.product.name if item.product else f'Producto {item.product_id}'} ({item.product.unit_measure if item.product else 'u'}) x {item.quantity:g}" for item in s.items)] for s in query.all()], "ventas"
    if kind == "compras":
        query = _date_filter(scope_query_to_company(PurchaseOrder.query.order_by(PurchaseOrder.date.desc()), PurchaseOrder), PurchaseOrder.date, start_utc, end_utc)
        return [["ID", "Proveedor", "Total", "Fecha"]] + [[p.id, p.supplier.name if p.supplier else "", p.total_amount or 0, format_local_datetime(p.date, tz_name)] for p in query.all()], "compras"
    if kind == "gastos":
        query = _date_filter(scope_query_to_company(Expense.query.order_by(Expense.date.desc()), Expense), Expense.date, start_utc, end_utc)
        return [["ID", "Categoria", "Descripcion", "Importe", "Fecha"]] + [[e.id, e.category, e.description, e.amount, format_local_datetime(e.date, tz_name)] for e in query.all()], "gastos"
    if kind == "caja":
        query = _date_filter(scope_query_to_company(CashMovement.query.order_by(CashMovement.created_at.desc()), CashMovement), CashMovement.created_at, start_utc, end_utc)
        return [["ID", "Tipo", "Categoria", "Importe", "Fecha"]] + [[m.id, m.movement_type, m.category or "", m.amount, format_local_datetime(m.created_at, tz_name)] for m in query.all()], "caja"
    if kind == "clientes":
        return [["ID", "Nombre", "Email", "WhatsApp", "Saldo", "Credito"]] + [[c.id, c.name, c.email or "", c.whatsapp or "", c.balance or 0, c.credit_limit or 0] for c in scope_query_to_company(Client.query.order_by(Client.name), Client).all()], "clientes"
    if kind == "productos":
        return [["ID", "Codigo", "Nombre", "Marca", "Unidad", "Stock", "Precio", "Costo"]] + [[p.id, p.barcode, p.name, p.brand or "", p.unit_measure or "", p.stock or 0, p.price or 0, p.cost_price or 0] for p in scope_query_to_company(Product.query.order_by(Product.name), Product).all()], "productos"
    if kind == "stock":
        return [["Codigo", "Producto", "Stock", "Minimo", "Unidad", "Estado"]] + [[p.barcode, p.name, p.stock or 0, p.min_stock or 0, p.unit_measure or "", "critico" if (p.stock or 0) <= (p.min_stock or 0) else "ok"] for p in scope_query_to_company(Product.query.order_by(Product.name), Product).all()], "stock"
    return [["Error"], ["Reporte no reconocido"]], "reporte"


def _date_filter(query, column, start, end):
    if start is not None:
        query = query.filter(column >= start)
    if end is not None:
        query = query.filter(column < end)
    return query
