"""Ticket and receipt rendering helpers for sales."""

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from stockarmobile.helpers.dates import format_local_datetime


class ReceiptService:
    @staticmethod
    def _sale_datetime(sale):
        company = getattr(sale, "company", None)
        tz_name = getattr(company, "timezone", None) or "America/Argentina/Buenos_Aires"
        return format_local_datetime(sale.date, tz_name, "%Y-%m-%d %H:%M") if sale.date else ""

    @staticmethod
    def _line_values(item):
        quantity = Decimal(str(getattr(item, "quantity", 0) or 0))
        price = Decimal(str(getattr(item, "price", 0) or 0))
        discount = Decimal(str(getattr(item, "discount", 0) or 0))
        gross = max(price * quantity, Decimal("0.00")).quantize(Decimal("0.01"))
        discount = min(max(discount, Decimal("0.00")), gross).quantize(Decimal("0.01"))
        total = (gross - discount).quantize(Decimal("0.01"))
        return quantity, price, discount, gross, total

    @staticmethod
    def _line_discount_total(sale):
        total = Decimal("0.00")
        for item in getattr(sale, "items", []) or []:
            total += ReceiptService._line_values(item)[2]
        return total.quantize(Decimal("0.01"))

    @staticmethod
    def _effective_discount(sale):
        stored = Decimal(str(getattr(sale, "discount", 0) or 0))
        line_total = ReceiptService._line_discount_total(sale)
        # Las promociones son descuentos de línea. Esto mantiene el ticket
        # correcto incluso para ventas antiguas que no persistieron el total.
        return max(stored, line_total).quantize(Decimal("0.01"))

    @staticmethod
    def wholesale_discount_info(item):
        """Return stored savings; infer legacy rows only when the current rule matches the charged price."""
        try:
            quantity = Decimal(str(getattr(item, "quantity", 0) or 0))
            unit_price = Decimal(str(getattr(item, "price", 0) or 0)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            percent_raw = getattr(item, "wholesale_discount_percent", None)
            reference_raw = getattr(item, "wholesale_reference_price", None)
            percent = Decimal(str(percent_raw)) if percent_raw not in (None, "") else None
            reference_price = Decimal(str(reference_raw)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ) if reference_raw not in (None, "") else None
        except (InvalidOperation, TypeError, ValueError):
            return None

        if percent is None or reference_price is None:
            if (
                str(getattr(item, "price_source", "") or "").strip().lower() != "mayorista"
                or not getattr(item, "price_list_id", None)
                or not getattr(item, "product_id", None)
                or quantity <= 0
            ):
                return None
            product = getattr(item, "product", None)
            if product is None:
                return None
            try:
                from wholesale_pricing import PriceListItem

                query = PriceListItem.query.filter(
                    PriceListItem.price_list_id == int(item.price_list_id),
                    PriceListItem.product_id == int(item.product_id),
                    PriceListItem.active.is_(True),
                    PriceListItem.min_quantity <= quantity,
                )
                sale = getattr(item, "sale", None)
                if getattr(sale, "company_id", None) is not None:
                    query = query.filter(PriceListItem.company_id == int(sale.company_id))
                rule = query.order_by(
                    PriceListItem.min_quantity.desc(), PriceListItem.id.desc()
                ).first()
                if rule is None or getattr(rule, "discount_percent", None) is None:
                    return None
                percent = Decimal(str(rule.discount_percent))
                reference_price = Decimal(str(getattr(product, "price", 0) or 0)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                expected_price = (
                    reference_price * (Decimal("100") - percent) / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                # Safe fallback for tickets created before snapshots existed: never
                # infer a discount unless it exactly reproduces the saved charged price.
                if expected_price != unit_price:
                    return None
            except Exception:
                return None

        if (
            percent is None or not percent.is_finite()
            or percent <= 0 or percent >= Decimal("100")
            or reference_price is None or reference_price <= unit_price
            or quantity <= 0
        ):
            return None
        unit_saving = (reference_price - unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        amount = (unit_saving * quantity).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if amount <= 0:
            return None
        return {
            "percent": percent,
            "reference_price": reference_price,
            "unit_saving": unit_saving,
            "amount": amount,
        }

    @staticmethod
    def wholesale_discount_total(sale):
        total = sum(
            (
                (ReceiptService.wholesale_discount_info(item) or {}).get("amount", Decimal("0.00"))
                for item in (getattr(sale, "items", []) or [])
            ),
            Decimal("0.00"),
        )
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _ticket_lines(sale):
        lines = []
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            quantity, price, discount, gross, total = ReceiptService._line_values(item)
            lines.append(f"{name}: $" + f"{price:.2f} x {quantity:g} {unit_measure} = $" + f"{gross:.2f}")
            wholesale_info = ReceiptService.wholesale_discount_info(item)
            if wholesale_info:
                lines.append(
                    "  Descuento mayorista (" + f"{wholesale_info['percent']:g}%): -$"
                    + f"{wholesale_info['amount']:.2f} (ya aplicado en el precio)"
                )
            if discount > 0:
                lines.append("  Descuento promoción: -$" + f"{discount:.2f}")
            if discount > 0 or wholesale_info:
                lines.append("  Total línea: $" + f"{total:.2f}")
        return lines

    @staticmethod
    def charge_rows(sale):
        """Return the persisted commercial charge snapshot for ticket rendering."""
        raw = getattr(sale, "charges_json", None)
        if not raw:
            return []
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []

        rows = []
        for charge in payload:
            if not isinstance(charge, dict):
                continue
            try:
                amount = Decimal(str(charge.get("amount") or 0)).quantize(Decimal("0.01"))
            except Exception:
                continue
            if amount <= Decimal("0.00"):
                continue
            charge_type = str(charge.get("type") or "fixed").strip().lower()
            try:
                value = Decimal(str(charge.get("value"))) if charge.get("value") is not None else None
            except Exception:
                value = None
            rows.append({
                "name": str(charge.get("name") or "Cargo").strip()[:120] or "Cargo",
                "type": charge_type,
                "value": value.quantize(Decimal("0.01")) if value is not None else None,
                "amount": amount,
                "base": str(charge.get("base") or "").strip(),
            })
        return rows

    @staticmethod
    def _snapshot_charge_amount(sale):
        return sum((row["amount"] for row in ReceiptService.charge_rows(sale)), Decimal("0.00")).quantize(Decimal("0.01"))

    @staticmethod
    def _adjustment_lines(label, adjustment_type, value, amount, reason, sign=""):
        if adjustment_type == "percentage" and value is not None:
            lines = [f"{label}: {value:.2f}%", f"{label} aplicado: {sign}${amount:.2f}"]
        elif adjustment_type == "fixed" and value is not None:
            lines = [f"{label}: {sign}${value:.2f}"]
        else:
            lines = [f"{label}: {sign}${amount:.2f}"]
        if reason:
            lines.append(f"Motivo {label.lower()}: {reason}")
        return lines

    @staticmethod
    def _json_object(raw):
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def ticket_context(*, company=None, sale=None):
        """Resolve receipt identity and printer overrides using the sale's persisted branch."""
        from app import Branch, Company

        company_id = getattr(sale, "company_id", None) or getattr(company, "id", None)
        if company is None and company_id:
            company = Company.query.filter_by(id=int(company_id)).first()

        branch = None
        branch_id = getattr(sale, "branch_id", None) if sale is not None else None
        if company is not None and branch_id:
            branch = Branch.query.filter_by(
                id=int(branch_id),
                company_id=int(company.id),
            ).first()

        company_printer = ReceiptService._json_object(
            getattr(company, "printer_settings_json", None)
        )
        company_preferences = ReceiptService._json_object(
            getattr(company, "preferences_json", None)
        )
        branch_settings = ReceiptService._json_object(
            getattr(branch, "receipt_settings_json", None)
        )
        billing = company_preferences.get("billing_business")
        if not isinstance(billing, dict):
            billing = {}
        print_template = billing.get("print_template")
        if not isinstance(print_template, dict):
            print_template = {}

        def text_value(value):
            return str(value or "").strip()

        def branch_override(key, fallback=""):
            configured = text_value(branch_settings.get(key))
            return configured if configured else text_value(fallback)

        company_name = text_value(getattr(company, "name", None)) or "STOCK ARMOBILE"
        if branch is not None and not bool(getattr(branch, "is_default", False)):
            default_ticket_name = text_value(getattr(branch, "name", None)) or company_name
        else:
            default_ticket_name = (
                text_value(company_printer.get("ticket_name"))
                or text_value(company_printer.get("printer_name"))
                or company_name
            )
        ticket_name = branch_override("ticket_name", default_ticket_name)[:120] or "STOCK ARMOBILE"

        address = branch_override(
            "address",
            getattr(branch, "address", None) or getattr(company, "address", None),
        )
        city = branch_override(
            "city",
            getattr(branch, "city", None) or getattr(company, "city", None),
        )
        province = branch_override(
            "province",
            getattr(branch, "province", None) or getattr(company, "province", None),
        )
        phone = branch_override(
            "phone",
            getattr(branch, "phone", None) or getattr(company, "phone", None),
        )
        whatsapp = branch_override(
            "whatsapp",
            getattr(branch, "whatsapp", None) or getattr(company, "whatsapp", None),
        )
        website = branch_override("website", getattr(company, "website", None))
        logo = branch_override("logo", getattr(company, "logo", None))
        footer = branch_override(
            "footer",
            company_printer.get("ticket_footer") or print_template.get("footer"),
        )

        printer_settings = dict(company_printer)
        printer_settings.setdefault("paper_size", "Ticket80")
        for key in (
            "printer_name",
            "printer_type",
            "printer_host",
            "printer_port",
            "paper_size",
        ):
            configured = text_value(branch_settings.get(key))
            if configured:
                raw_value = branch_settings.get(key)
                if key == "printer_port":
                    try:
                        port = int(raw_value)
                    except (TypeError, ValueError):
                        continue
                    if not 1 <= port <= 65535:
                        continue
                    printer_settings[key] = port
                else:
                    printer_settings[key] = configured
        if "cashdrawer_enabled" in branch_settings:
            printer_settings["cashdrawer_enabled"] = bool(branch_settings.get("cashdrawer_enabled"))

        location_parts = [part for part in (address, city, province) if part]
        return {
            "branch_id": getattr(branch, "id", None),
            "branch_name": text_value(getattr(branch, "name", None)),
            "ticket_name": ticket_name,
            "logo_url": logo,
            "address": address,
            "city": city,
            "province": province,
            "location_line": ", ".join(location_parts),
            "phone": phone,
            "whatsapp": whatsapp,
            "website": website,
            "footer": footer,
            "printer_settings": printer_settings,
        }

    @staticmethod
    def ticket_logo_bytes(*, company=None, context=None):
        """Resolve an uploaded or local static logo without fetching arbitrary URLs."""
        import os
        import mimetypes
        from flask import current_app, has_app_context

        logo_url = str((context or {}).get("logo_url") or "").strip()
        if not logo_url:
            return None

        if (
            company is not None
            and logo_url == str(getattr(company, "logo", None) or "").strip()
        ):
            payload = getattr(company, "logo_data", None)
            mime_type = str(getattr(company, "logo_mime_type", None) or "").strip().lower()
            if payload and mime_type.startswith("image/") and len(payload) <= 3 * 1024 * 1024:
                return bytes(payload)

        # Branch-specific logos may reference a public static upload. Do not
        # fetch remote URLs or arbitrary local paths (SSRF/path traversal).
        if not has_app_context() or not logo_url.startswith("/static/"):
            return None
        relative_path = logo_url[len("/static/"):]
        static_root = os.path.realpath(current_app.static_folder or "")
        candidate = os.path.realpath(os.path.join(static_root, relative_path))
        try:
            if os.path.commonpath((static_root, candidate)) != static_root:
                return None
            if not os.path.isfile(candidate) or os.path.getsize(candidate) > 3 * 1024 * 1024:
                return None
            mime_type = (mimetypes.guess_type(candidate)[0] or "").lower()
            if not mime_type.startswith("image/"):
                return None
            with open(candidate, "rb") as handle:
                return handle.read()
        except (OSError, ValueError):
            return None

    @staticmethod
    def ticket_brand_name(*, company, branch=None):
        fallback = "STOCK ARMOBILE"
        if branch is not None and not bool(getattr(branch, "is_default", False)):
            return (getattr(branch, "name", None) or fallback)[:120]
        if company is None:
            return fallback
        settings = ReceiptService._json_object(getattr(company, "printer_settings_json", None))
        name = (
            settings.get("ticket_name")
            or settings.get("printer_name")
            or getattr(company, "name", "")
            or fallback
        )
        return str(name).strip()[:120] or fallback

    @staticmethod
    def _ticket_context_lines(context):
        lines = []
        if context.get("branch_name"):
            lines.append(f"Sucursal: {context['branch_name']}")
        if context.get("location_line"):
            lines.append(f"Dirección: {context['location_line']}")
        if context.get("phone"):
            lines.append(f"Teléfono: {context['phone']}")
        if context.get("whatsapp"):
            lines.append(f"WhatsApp: {context['whatsapp']}")
        if context.get("website"):
            lines.append(f"Web: {context['website']}")
        return lines

    @staticmethod
    def ticket_note(sale, ticket_brand=None):
        """Translate an internal AI workflow marker into a merchant-facing note."""
        note = str(getattr(sale, "note", None) or "").strip()
        if note != "Pedido generado por el Vendedor 24 hs de StockARmobile.":
            return note
        company = getattr(sale, "company", None)
        business_name = str(
            getattr(company, "name", None)
            or ticket_brand
            or "el comercio"
        ).strip()
        return f"Pedido generado por el Vendedor IA de {business_name}."

    @staticmethod
    def ticket_text(sale, ticket_brand):
        context = ReceiptService.ticket_context(sale=sale)
        brand = (ticket_brand or context["ticket_name"] or "STOCK ARMOBILE").strip()
        sale_datetime = ReceiptService._sale_datetime(sale)
        lines = [f"{brand} - TICKET DE VENTA", "-" * 32, f"Venta: #{sale.id}", f"Fecha: {sale_datetime}"]
        lines.extend(ReceiptService._ticket_context_lines(context))
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("-" * 32)
        lines.extend(ReceiptService._ticket_lines(sale))
        ticket_note = ReceiptService.ticket_note(sale, ticket_brand=brand)
        if ticket_note:
            lines.extend(["-" * 32, f"Obs.: {ticket_note}"])
        effective_discount = ReceiptService._effective_discount(sale)
        wholesale_savings = ReceiptService.wholesale_discount_total(sale)
        lines.extend(["-" * 32, f"Subtotal: ${sale.subtotal:.2f}"])
        if wholesale_savings > 0:
            lines.append(f"Ahorro mayorista ya incluido en precios unitarios: ${wholesale_savings:.2f}")
        discount_label = "Descuento adicional" if wholesale_savings > 0 else "Descuento"
        lines.extend(ReceiptService._adjustment_lines(discount_label, sale.discount_type, sale.discount_value, effective_discount, sale.discount_reason, "-"))
        charge_rows = ReceiptService.charge_rows(sale)
        if charge_rows:
            for row in charge_rows:
                if row["type"] == "percentage" and row["value"] is not None:
                    lines.append(f'{row["name"]}: {row["value"]:.2f}%')
                    lines.append(f'{row["name"]} aplicado: ${row["amount"]:.2f}')
                else:
                    lines.append(f'{row["name"]}: +${row["amount"]:.2f}')
            expected_order_charges = (Decimal(str(getattr(sale, "surcharge", 0) or 0)) + Decimal(str(getattr(sale, "tax", 0) or 0))).quantize(Decimal("0.01"))
            covered = ReceiptService._snapshot_charge_amount(sale)
            if covered < expected_order_charges:
                remainder = expected_order_charges - covered
                surcharge = Decimal(str(getattr(sale, "surcharge", 0) or 0)).quantize(Decimal("0.01"))
                extra_surcharge = min(remainder, surcharge)
                if extra_surcharge > 0:
                    lines.append(f"Recargo adicional: +${extra_surcharge:.2f}")
                    remainder -= extra_surcharge
                if remainder > 0 and getattr(sale, "tax", 0):
                    lines.append(f"Impuestos adicionales: +${remainder:.2f}")
        else:
            if sale.surcharge:
                lines.extend(ReceiptService._adjustment_lines("Recargo", sale.surcharge_type, sale.surcharge_value, sale.surcharge, sale.surcharge_reason))
            if sale.tax:
                lines.append(f"Impuestos: ${sale.tax:.2f}")
        lines.extend(["=" * 32, f"TOTAL: ${sale.total_amount:.2f}"])
        if context.get("footer"):
            lines.append(context["footer"])
        lines.append("Gracias por su compra!")
        return "\n".join(lines)

    @staticmethod
    def ticket_rows(sale):
        rows = []
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            quantity, price, discount, gross, total = ReceiptService._line_values(item)
            wholesale_info = ReceiptService.wholesale_discount_info(item)
            rows.append({
                "name": name,
                "unit_measure": unit_measure,
                "quantity": quantity,
                "price": price,
                "discount": discount,
                "gross": gross,
                "total": total,
                "wholesale_discount_percent": wholesale_info["percent"] if wholesale_info else None,
                "wholesale_reference_price": wholesale_info["reference_price"] if wholesale_info else None,
                "wholesale_unit_saving": wholesale_info["unit_saving"] if wholesale_info else Decimal("0.00"),
                "wholesale_discount_amount": wholesale_info["amount"] if wholesale_info else Decimal("0.00"),
            })
        return rows

    @staticmethod
    def whatsapp_text(sale, ticket_brand):
        context = ReceiptService.ticket_context(sale=sale)
        brand = (ticket_brand or context["ticket_name"] or "STOCK ARMOBILE").strip()
        sale_datetime = ReceiptService._sale_datetime(sale)
        lines = [f"{brand} - Ticket de compra", f"Venta #{sale.id}", f"Fecha: {sale_datetime}"]
        lines.extend(ReceiptService._ticket_context_lines(context))
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("------------------------------")
        lines.extend(ReceiptService._ticket_lines(sale))
        effective_discount = ReceiptService._effective_discount(sale)
        wholesale_savings = ReceiptService.wholesale_discount_total(sale)
        lines.extend(["------------------------------", f"Subtotal: ${sale.subtotal:.2f}"])
        if wholesale_savings > 0:
            lines.append(f"Ahorro mayorista ya incluido en precios unitarios: ${wholesale_savings:.2f}")
        discount_label = "Descuento adicional" if wholesale_savings > 0 else "Descuento"
        lines.extend(ReceiptService._adjustment_lines(discount_label, sale.discount_type, sale.discount_value, effective_discount, sale.discount_reason, "-"))
        charge_rows = ReceiptService.charge_rows(sale)
        if charge_rows:
            for row in charge_rows:
                if row["type"] == "percentage" and row["value"] is not None:
                    lines.append(f'{row["name"]}: {row["value"]:.2f}%')
                    lines.append(f'{row["name"]} aplicado: ${row["amount"]:.2f}')
                else:
                    lines.append(f'{row["name"]}: +${row["amount"]:.2f}')
        else:
            if sale.surcharge:
                lines.extend(ReceiptService._adjustment_lines("Recargo", sale.surcharge_type, sale.surcharge_value, sale.surcharge, sale.surcharge_reason))
            if sale.tax:
                lines.append(f"Impuestos: ${sale.tax:.2f}")
        lines.append(f"Total: ${sale.total_amount:.2f}")
        if context.get("footer"):
            lines.append(context["footer"])
        lines.append("Gracias por su compra!")
        return "\n".join(lines)
