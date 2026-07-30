"""Business billing module service.

Centralizes business invoice settings, dashboard metrics, document center,
and report/export helpers. This service is intentionally isolated from SaaS
subscription and payment concerns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
import csv

from sqlalchemy.exc import IntegrityError

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


@dataclass
class BillingStatusBadge:
    key: str
    label: str
    css_class: str


class BusinessBillingService:
    DOCUMENT_TYPES = [
        ("factura_a", "Factura A"),
        ("factura_b", "Factura B"),
        ("factura_c", "Factura C"),
        ("nota_credito", "Nota de Credito"),
        ("nota_debito", "Nota de Debito"),
        ("remito", "Remito"),
        ("recibo", "Recibo"),
        ("presupuesto", "Presupuesto"),
        ("ticket", "Ticket"),
        ("orden_compra", "Orden de Compra"),
    ]

    STATUS_BADGES = {
        "emitido": BillingStatusBadge("emitido", "Emitido", "text-bg-success"),
        "pendiente": BillingStatusBadge("pendiente", "Pendiente", "text-bg-warning"),
        "pagado": BillingStatusBadge("pagado", "Pagado", "text-bg-success"),
        "anulado": BillingStatusBadge("anulado", "Anulado", "text-bg-danger"),
        "borrador": BillingStatusBadge("borrador", "Borrador", "text-bg-secondary"),
        "enviado": BillingStatusBadge("enviado", "Enviado", "text-bg-primary"),
        "vencido": BillingStatusBadge("vencido", "Vencido", "text-bg-danger"),
    }

    @staticmethod
    def _json_dict(raw_value):
        raw = (raw_value or "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _normalize_pos_number(raw_value):
        digits = "".join(ch for ch in str(raw_value or "") if ch.isdigit())
        if not digits:
            return "00001"
        return digits[-5:].zfill(5)

    @staticmethod
    def default_config(company):
        enabled_docs = {
            key: key in {"factura_b", "factura_c", "presupuesto", "recibo", "ticket"}
            for key, _label in BusinessBillingService.DOCUMENT_TYPES
        }
        numbering = {
            key: "00001-00000001" for key, _label in BusinessBillingService.DOCUMENT_TYPES
        }
        return {
            "fiscal": {
                "tax_id": (getattr(company, "tax_id", None) or "").strip(),
                "legal_name": (getattr(company, "legal_name", None) or getattr(company, "name", None) or "").strip(),
                "iva_condition": "Consumidor Final",
                "gross_income": "",
                "jurisdiction": "",
                "activity_start": "",
                "fiscal_address": (getattr(company, "address", None) or "").strip(),
                "branch_name": "Casa central",
            },
            "documents_enabled": enabled_docs,
            "points_of_sale": [
                {
                    "number": "00001",
                    "description": "Casa central",
                    "branch": "Casa central",
                    "active": True,
                }
            ],
            "active_pos": "00001",
            "numbering": numbering,
            "template": {
                "logo": (getattr(company, "logo", None) or "").strip(),
                "footer": "Gracias por su compra.",
                "commercial_terms": "Pago contado.",
                "observations": "",
                "show_qr": True,
                "show_barcode": False,
                "format_a4": True,
                "format_ticket_58": False,
                "format_ticket_80": True,
            },
            "emission": {
                "auto_numbering": True,
                "auto_print": False,
                "send_pdf_email": False,
                "send_whatsapp_prepared": True,
                "copies": 1,
                "currency": "ARS",
                "decimals": 2,
                "default_format": "a4",
                "default_template": "estandar",
            },
            "electronic": {
                "status": "coming_soon",
                "connected": False,
                "certificate": "",
                "certificate_expires_at": "",
                "environment": "homologacion",
                "cae": "",
                "caea": "",
                "enabled_points": [],
                "provider": "arca",
            },
        }

    @staticmethod
    def load_config(company):
        base = BusinessBillingService.default_config(company)
        prefs = BusinessBillingService._json_dict(getattr(company, "preferences_json", None))
        stored = prefs.get("billing_business")
        if not isinstance(stored, dict):
            stored = {}

        fiscal = base["fiscal"].copy()
        fiscal.update(stored.get("fiscal") or {})

        docs = base["documents_enabled"].copy()
        docs.update(stored.get("documents_enabled") or {})
        docs = {key: bool(docs.get(key)) for key, _label in BusinessBillingService.DOCUMENT_TYPES}

        points = []
        points_raw = stored.get("points_of_sale")
        if isinstance(points_raw, list):
            for item in points_raw:
                if not isinstance(item, dict):
                    continue
                number = BusinessBillingService._normalize_pos_number(item.get("number"))
                points.append(
                    {
                        "number": number,
                        "description": (str(item.get("description") or "").strip() or f"Punto de venta {number}")[:80],
                        "branch": (str(item.get("branch") or "").strip() or "Casa central")[:80],
                        "active": bool(item.get("active", False)),
                    }
                )
        if not points:
            points = base["points_of_sale"]

        active_pos = BusinessBillingService._normalize_pos_number(stored.get("active_pos") or points[0].get("number"))
        any_active = False
        for point in points:
            point["active"] = point.get("number") == active_pos
            any_active = any_active or point["active"]
        if not any_active and points:
            points[0]["active"] = True
            active_pos = points[0]["number"]

        numbering = base["numbering"].copy()
        numbering.update(stored.get("numbering") or {})
        for key, _label in BusinessBillingService.DOCUMENT_TYPES:
            value = str(numbering.get(key) or "").strip()
            numbering[key] = value if value else f"{active_pos}-00000001"

        template = base["template"].copy()
        template.update(stored.get("template") or stored.get("print_template") or {})
        template["show_qr"] = bool(template.get("show_qr"))
        template["show_barcode"] = bool(template.get("show_barcode"))
        template["format_a4"] = bool(template.get("format_a4", True))
        template["format_ticket_58"] = bool(template.get("format_ticket_58"))
        template["format_ticket_80"] = bool(template.get("format_ticket_80", True))

        emission = base["emission"].copy()
        emission.update(stored.get("emission") or {})
        emission["auto_numbering"] = bool(emission.get("auto_numbering", True))
        emission["auto_print"] = bool(emission.get("auto_print"))
        emission["send_pdf_email"] = bool(emission.get("send_pdf_email"))
        emission["send_whatsapp_prepared"] = bool(emission.get("send_whatsapp_prepared", True))
        emission["copies"] = max(1, int(emission.get("copies") or 1))
        emission["decimals"] = max(0, min(4, int(emission.get("decimals") or 2)))

        electronic = base["electronic"].copy()
        electronic.update(stored.get("electronic") or {})
        electronic["connected"] = bool(electronic.get("connected"))
        electronic["status"] = str(electronic.get("status") or "coming_soon")
        electronic["environment"] = str(electronic.get("environment") or "homologacion")

        return {
            "fiscal": fiscal,
            "documents_enabled": docs,
            "points_of_sale": points,
            "active_pos": active_pos,
            "numbering": numbering,
            "template": template,
            "emission": emission,
            "electronic": electronic,
        }

    @staticmethod
    def save_config(company, config):
        prefs = BusinessBillingService._json_dict(getattr(company, "preferences_json", None))
        prefs["billing_business"] = config
        company.preferences_json = json.dumps(prefs)

    @staticmethod
    def active_pos_row(config):
        active_pos = config.get("active_pos")
        for row in config.get("points_of_sale") or []:
            if row.get("number") == active_pos:
                return row
        return None

    @staticmethod
    def document_label(raw_type):
        normalized = (raw_type or "").strip().lower()
        mapping = {key: label for key, label in BusinessBillingService.DOCUMENT_TYPES}
        if normalized in mapping:
            return mapping[normalized]
        return normalized.replace("_", " ").title() or "Comprobante"

    @staticmethod
    def status_badge(status_key):
        return BusinessBillingService.STATUS_BADGES.get(
            (status_key or "").strip().lower(),
            BillingStatusBadge("pendiente", "Pendiente", "text-bg-warning"),
        )

    @staticmethod
    def _sale_status_key(sale):
        raw_status = (getattr(sale, "status", "") or "").strip().lower()
        if raw_status in {"anulada", "cancelada", "rechazada"}:
            return "anulado"
        if raw_status in {"borrador"}:
            return "borrador"
        if bool(getattr(sale, "comprobante_emitido", False)):
            if raw_status in {"pagada", "paid"}:
                return "pagado"
            return "emitido"
        if bool(getattr(sale, "requiere_comprobante", False)):
            return "pendiente"
        return "emitido"

    @staticmethod
    def _quote_status_key(quote):
        raw = (getattr(quote, "status", "") or "").strip().upper()
        if raw in {"ANULADO", "RECHAZADO"}:
            return "anulado"
        if raw == "VENCIDO":
            return "vencido"
        if raw in {"BORRADOR"}:
            return "borrador"
        if raw in {"ENVIADO"}:
            return "enviado"
        if raw in {"PENDIENTE"}:
            return "pendiente"
        if raw in {"APROBADO", "CONVERTIDO"}:
            return "emitido"
        return "pendiente"

    @staticmethod
    def _month_window(now):
        start = datetime(now.year, now.month, 1)
        if now.month == 12:
            end = datetime(now.year + 1, 1, 1)
        else:
            end = datetime(now.year, now.month + 1, 1)
        return start, end

    @staticmethod
    def _safe_float(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _split_counter_from_number(number_value):
        raw = (number_value or "").strip()
        if "-" not in raw:
            return 1, 8
        _left, right = raw.split("-", 1)
        digits = "".join(ch for ch in right if ch.isdigit())
        if not digits:
            return 1, 8
        try:
            return int(digits), len(digits)
        except ValueError:
            return 1, 8

    @staticmethod
    def _format_document_number(pos_number, seq_number, width):
        pos = BusinessBillingService._normalize_pos_number(pos_number)
        return f"{pos}-{str(int(seq_number)).zfill(max(1, int(width)))}"

    @staticmethod
    def issue_sale_document(db_session, *, company, sale, config, emitted_by_user_id=None, metadata=None):
        from app import BusinessDocument, BusinessDocumentSequence, utcnow

        existing = (
            db_session.query(BusinessDocument)
            .filter_by(company_id=company.id, source_type="sale", source_id=sale.id)
            .order_by(BusinessDocument.id.desc())
            .first()
        )
        if existing is not None and (existing.status or "").strip().lower() != "anulado":
            return existing

        doc_type = ((getattr(sale, "tipo_comprobante", None) or "factura_b") or "factura_b").strip().lower()
        active_pos = BusinessBillingService._normalize_pos_number(config.get("active_pos") or "00001")
        seed_value, width = BusinessBillingService._split_counter_from_number(
            (config.get("numbering") or {}).get(doc_type)
        )

        sequence = (
            db_session.query(BusinessDocumentSequence)
            .filter_by(company_id=company.id, doc_type=doc_type, pos_number=active_pos)
            .with_for_update()
            .first()
        )
        if sequence is None:
            sequence = BusinessDocumentSequence(
                company_id=company.id,
                doc_type=doc_type,
                pos_number=active_pos,
                current_number=max(seed_value - 1, 0),
            )
            db_session.add(sequence)
            db_session.flush()

        sequence.current_number = int(sequence.current_number or 0) + 1
        seq_number = int(sequence.current_number)
        document_number = BusinessBillingService._format_document_number(active_pos, seq_number, width)

        status_key = BusinessBillingService._sale_status_key(sale)
        if status_key == "pendiente":
            status_key = "emitido"
        if status_key not in BusinessBillingService.STATUS_BADGES:
            status_key = "emitido"

        payload = metadata if isinstance(metadata, dict) else {}
        document = BusinessDocument(
            company_id=company.id,
            source_type="sale",
            source_id=sale.id,
            doc_type=doc_type,
            pos_number=active_pos,
            seq_number=seq_number,
            document_number=document_number,
            status=status_key,
            client_name=(getattr(sale, "customer", None) or "Consumidor final")[:200],
            total_amount=(sale.total_amount or Decimal("0.00")),
            currency=((config.get("emission") or {}).get("currency") or "ARS"),
            branch_label=str(getattr(sale, "branch_id", None) or (BusinessBillingService.active_pos_row(config) or {}).get("branch") or "Casa central")[:120],
            emitted_by_user_id=emitted_by_user_id,
            metadata_json=json.dumps(payload, ensure_ascii=False) if payload else None,
            issued_at=utcnow(),
        )
        db_session.add(document)

        sale.comprobante_emitido = True
        if not getattr(sale, "tipo_comprobante", None):
            sale.tipo_comprobante = doc_type

        numbering = config.get("numbering") or {}
        numbering[doc_type] = BusinessBillingService._format_document_number(active_pos, seq_number + 1, width)
        config["numbering"] = numbering
        return document

    @staticmethod
    def mark_sale_document_annulled(db_session, *, company_id, sale_id):
        from app import BusinessDocument, utcnow

        document = (
            db_session.query(BusinessDocument)
            .filter_by(company_id=company_id, source_type="sale", source_id=sale_id)
            .order_by(BusinessDocument.id.desc())
            .first()
        )
        if document is None:
            return None
        document.status = "anulado"
        document.annulled_at = utcnow()
        return document

    @staticmethod
    def build_dashboard(company_id, config, *, now=None):
        from app import BusinessDocument, Quote, Sale, utcnow

        current = now or utcnow()
        month_start, month_end = BusinessBillingService._month_window(current)

        issued_docs = (
            BusinessDocument.query.filter_by(company_id=company_id)
            .filter(BusinessDocument.issued_at >= month_start, BusinessDocument.issued_at < month_end)
            .filter(BusinessDocument.status != "anulado")
            .all()
        )
        total_billed_month = sum(BusinessBillingService._safe_float(item.total_amount) for item in issued_docs)
        issued_count = len(issued_docs)

        pending_documents = (
            Sale.query.filter_by(company_id=company_id)
            .filter(Sale.requiere_comprobante.is_(True), Sale.comprobante_emitido.is_(False))
            .count()
        )
        pending_quotes = (
            Quote.query.filter_by(company_id=company_id)
            .filter(Quote.status.in_(["BORRADOR", "ENVIADO", "PENDIENTE", "APROBADO"]))
            .count()
        )

        recent_sale = (
            BusinessDocument.query.filter_by(company_id=company_id)
            .order_by(BusinessDocument.issued_at.desc(), BusinessDocument.id.desc())
            .first()
        )
        recent_quote = (
            Quote.query.filter_by(company_id=company_id)
            .order_by(Quote.date.desc(), Quote.id.desc())
            .first()
        )
        last_document = None
        if recent_sale and recent_quote:
            last_document = recent_sale if (recent_sale.date or datetime.min) >= (recent_quote.date or datetime.min) else recent_quote
        else:
            last_document = recent_sale or recent_quote

        last_document_label = None
        if last_document is not None:
            if hasattr(last_document, "document_number"):
                doc_type = last_document.doc_type or "factura_b"
                last_document_label = f"{BusinessBillingService.document_label(doc_type)} {last_document.document_number}"
            else:
                last_document_label = f"Presupuesto {last_document.number or ('#' + str(last_document.id))}"

        active_pos = BusinessBillingService.active_pos_row(config)
        active_pos_number = active_pos.get("number") if active_pos else "00001"
        current_number = config.get("numbering", {}).get("factura_b") or f"{active_pos_number}-00000001"

        electronic = config.get("electronic") or {}
        electronic_status = "Proximamente disponible"
        if electronic.get("connected"):
            env = "Produccion" if (electronic.get("environment") or "").strip().lower() == "produccion" else "Homologacion"
            electronic_status = f"Conectado ({env})"

        return {
            "total_billed_month": total_billed_month,
            "issued_count": issued_count,
            "last_document": last_document_label,
            "electronic_status": electronic_status,
            "active_pos": active_pos_number,
            "current_number": current_number,
            "pending_documents": pending_documents,
            "pending_quotes": pending_quotes,
        }

    @staticmethod
    def _build_sale_number(config, sale):
        active_pos = config.get("active_pos") or "00001"
        return f"{active_pos}-{int(getattr(sale, 'id', 0)):08d}"

    @staticmethod
    def _filter_period(value, date_from, date_to):
        if date_from and value and value < date_from:
            return False
        if date_to and value and value >= (date_to + timedelta(days=1)):
            return False
        return True

    @staticmethod
    def list_documents(company_id, config, filters):
        from app import BusinessDocument, Quote, Sale

        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        search_number = (filters.get("number") or "").strip().lower()
        search_client = (filters.get("client") or "").strip().lower()
        search_cuit = (filters.get("cuit") or "").strip().lower()
        selected_type = (filters.get("doc_type") or "").strip().lower()
        selected_status = (filters.get("status") or "").strip().lower()
        selected_branch = (filters.get("branch") or "").strip().lower()
        selected_user = int(filters.get("user_id") or 0)
        amount_min = filters.get("amount_min")
        amount_max = filters.get("amount_max")
        method = (filters.get("payment_method") or "").strip().lower()

        rows = []
        persisted_docs = (
            BusinessDocument.query.filter_by(company_id=company_id)
            .order_by(BusinessDocument.issued_at.desc(), BusinessDocument.id.desc())
            .limit(2000)
            .all()
        )
        persisted_sale = {}
        persisted_quote = {}
        for doc in persisted_docs:
            if (doc.source_type or "") == "sale" and doc.source_id not in persisted_sale:
                persisted_sale[int(doc.source_id)] = doc
            if (doc.source_type or "") == "quote" and doc.source_id not in persisted_quote:
                persisted_quote[int(doc.source_id)] = doc

        sales = (
            Sale.query.filter_by(company_id=company_id)
            .order_by(Sale.date.desc(), Sale.id.desc())
            .limit(600)
            .all()
        )
        for sale in sales:
            event_date = sale.date
            if not BusinessBillingService._filter_period(event_date, date_from, date_to):
                continue
            amount = BusinessBillingService._safe_float(sale.total_amount)
            persisted = persisted_sale.get(int(sale.id))
            doc_type_key = ((persisted.doc_type if persisted is not None else sale.tipo_comprobante) or "factura_b").strip().lower()
            status_key = ((persisted.status if persisted is not None else BusinessBillingService._sale_status_key(sale)) or "pendiente").strip().lower()
            number = persisted.document_number if persisted is not None else BusinessBillingService._build_sale_number(config, sale)
            branch_text = str((persisted.branch_label if persisted is not None else getattr(sale, "branch_id", None)) or "Casa central")
            user_name = getattr(getattr(sale, "seller", None), "username", None) or "-"
            client_text = ((persisted.client_name if persisted is not None else sale.customer) or "Consumidor final").strip()
            payment_method = (sale.payment_method or "").strip().lower()
            amount = BusinessBillingService._safe_float(persisted.total_amount if persisted is not None else sale.total_amount)

            if search_number and search_number not in number.lower() and search_number not in str(sale.id):
                continue
            if search_client and search_client not in client_text.lower():
                continue
            if search_cuit and search_cuit not in client_text.lower():
                continue
            if selected_type and selected_type != doc_type_key:
                continue
            if selected_status and selected_status != status_key:
                continue
            if selected_branch and selected_branch not in branch_text.lower():
                continue
            if selected_user and selected_user != int(getattr(sale, "seller_id", 0) or 0):
                continue
            if amount_min is not None and amount < amount_min:
                continue
            if amount_max is not None and amount > amount_max:
                continue
            if method and method not in payment_method:
                continue

            rows.append(
                {
                    "source": "sale",
                    "id": sale.id,
                    "date": event_date,
                    "number": number,
                    "client": client_text,
                    "doc_type_key": doc_type_key,
                    "doc_type": BusinessBillingService.document_label(doc_type_key),
                    "amount": amount,
                    "status_key": status_key,
                    "status_badge": BusinessBillingService.status_badge(status_key),
                    "branch": branch_text,
                    "user_name": user_name,
                    "payment_method": sale.payment_method or "-",
                    "can_emit": bool(getattr(sale, "requiere_comprobante", False) and not bool(getattr(sale, "comprobante_emitido", False))),
                    "can_annul": status_key != "anulado",
                    "can_duplicate": False,
                    "can_convert": False,
                    "can_email": True,
                    "can_whatsapp": True,
                }
            )

        quotes = (
            Quote.query.filter_by(company_id=company_id)
            .order_by(Quote.date.desc(), Quote.id.desc())
            .limit(400)
            .all()
        )
        for quote in quotes:
            event_date = quote.date
            if not BusinessBillingService._filter_period(event_date, date_from, date_to):
                continue
            amount = BusinessBillingService._safe_float(quote.total_amount)
            doc_type_key = "presupuesto"
            persisted = persisted_quote.get(int(quote.id))
            status_key = ((persisted.status if persisted is not None else BusinessBillingService._quote_status_key(quote)) or "pendiente").strip().lower()
            number = ((persisted.document_number if persisted is not None else quote.number) or f"P-{quote.id:06d}").strip()
            branch_text = str((persisted.branch_label if persisted is not None else getattr(quote, "branch_id", None)) or "Casa central")
            user_name = getattr(getattr(quote, "created_by_user", None), "username", None) or "-"
            client_text = (
                (persisted.client_name if persisted is not None else None)
                or getattr(getattr(quote, "client", None), "name", None)
                or quote.consumer_name
                or "Consumidor final"
            )
            amount = BusinessBillingService._safe_float(persisted.total_amount if persisted is not None else quote.total_amount)

            if search_number and search_number not in number.lower() and search_number not in str(quote.id):
                continue
            if search_client and search_client not in client_text.lower():
                continue
            if search_cuit and search_cuit not in client_text.lower():
                continue
            if selected_type and selected_type != doc_type_key:
                continue
            if selected_status and selected_status != status_key:
                continue
            if selected_branch and selected_branch not in branch_text.lower():
                continue
            if selected_user and selected_user != int(getattr(quote, "created_by_user_id", 0) or 0):
                continue
            if amount_min is not None and amount < amount_min:
                continue
            if amount_max is not None and amount > amount_max:
                continue
            if method:
                continue

            rows.append(
                {
                    "source": "quote",
                    "id": quote.id,
                    "date": event_date,
                    "number": number,
                    "client": client_text,
                    "doc_type_key": doc_type_key,
                    "doc_type": BusinessBillingService.document_label(doc_type_key),
                    "amount": amount,
                    "status_key": status_key,
                    "status_badge": BusinessBillingService.status_badge(status_key),
                    "branch": branch_text,
                    "user_name": user_name,
                    "payment_method": "-",
                    "can_emit": False,
                    "can_annul": status_key not in {"anulado", "emitido"},
                    "can_duplicate": True,
                    "can_convert": status_key in {"pendiente", "enviado", "borrador"},
                    "can_email": True,
                    "can_whatsapp": True,
                }
            )

        rows.sort(key=lambda item: item.get("date") or datetime.min, reverse=True)
        return rows

    @staticmethod
    def report_summary(rows):
        by_type = {}
        by_client = {}
        by_user = {}
        by_branch = {}
        total = 0.0
        for row in rows:
            amount = BusinessBillingService._safe_float(row.get("amount"))
            total += amount
            by_type[row.get("doc_type", "-")] = by_type.get(row.get("doc_type", "-"), 0.0) + amount
            by_client[row.get("client", "-")] = by_client.get(row.get("client", "-"), 0.0) + amount
            by_user[row.get("user_name", "-")] = by_user.get(row.get("user_name", "-"), 0.0) + amount
            by_branch[row.get("branch", "-")] = by_branch.get(row.get("branch", "-"), 0.0) + amount

        return {
            "total": total,
            "by_type": sorted(by_type.items(), key=lambda item: item[1], reverse=True),
            "by_client": sorted(by_client.items(), key=lambda item: item[1], reverse=True),
            "by_user": sorted(by_user.items(), key=lambda item: item[1], reverse=True),
            "by_branch": sorted(by_branch.items(), key=lambda item: item[1], reverse=True),
        }

    @staticmethod
    def export_csv(rows):
        output = BytesIO()
        text = output
        # csv module requires text stream, so encode after writing.
        import io

        text_stream = io.StringIO()
        writer = csv.writer(text_stream)
        writer.writerow(["Fecha", "Numero", "Cliente", "Tipo", "Importe", "Estado", "Sucursal", "Usuario", "Metodo pago"])
        for row in rows:
            writer.writerow(
                [
                    row.get("date").strftime("%Y-%m-%d %H:%M") if row.get("date") else "-",
                    row.get("number") or "-",
                    row.get("client") or "-",
                    row.get("doc_type") or "-",
                    f"{BusinessBillingService._safe_float(row.get('amount')):.2f}",
                    row.get("status_badge").label if row.get("status_badge") else "-",
                    row.get("branch") or "-",
                    row.get("user_name") or "-",
                    row.get("payment_method") or "-",
                ]
            )
        text.write(text_stream.getvalue().encode("utf-8"))
        text.seek(0)
        return text

    @staticmethod
    def export_excel(rows):
        book = Workbook()
        sheet = book.active
        sheet.title = "Comprobantes"
        headers = ["Fecha", "Numero", "Cliente", "Tipo", "Importe", "Estado", "Sucursal", "Usuario", "Metodo pago"]
        sheet.append(headers)
        for row in rows:
            sheet.append(
                [
                    row.get("date").strftime("%Y-%m-%d %H:%M") if row.get("date") else "-",
                    row.get("number") or "-",
                    row.get("client") or "-",
                    row.get("doc_type") or "-",
                    BusinessBillingService._safe_float(row.get("amount")),
                    row.get("status_badge").label if row.get("status_badge") else "-",
                    row.get("branch") or "-",
                    row.get("user_name") or "-",
                    row.get("payment_method") or "-",
                ]
            )
        for col in ["A", "B", "C", "D", "E", "F", "G", "H", "I"]:
            sheet.column_dimensions[col].width = 18

        output = BytesIO()
        book.save(output)
        output.seek(0)
        return output

    @staticmethod
    def export_pdf(rows, *, title="Centro de comprobantes"):
        output = BytesIO()
        pdf = canvas.Canvas(output, pagesize=A4)
        width, height = A4
        y = height - 40
        pdf.setTitle(title)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(32, y, title)
        y -= 20
        pdf.setFont("Helvetica", 8)
        for row in rows[:250]:
            if y < 40:
                pdf.showPage()
                y = height - 40
                pdf.setFont("Helvetica", 8)
            line = (
                f"{row.get('date').strftime('%Y-%m-%d') if row.get('date') else '-'} | "
                f"{row.get('number') or '-'} | {row.get('client') or '-'} | "
                f"{row.get('doc_type') or '-'} | ${BusinessBillingService._safe_float(row.get('amount')):.2f} | "
                f"{row.get('status_badge').label if row.get('status_badge') else '-'}"
            )
            pdf.drawString(24, y, line[:150])
            y -= 12

        pdf.save()
        output.seek(0)
        return output

    @staticmethod
    def parse_filter_values(args):
        def _parse_date(value):
            raw = (value or "").strip()
            if not raw:
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                return None

        def _parse_float(value):
            raw = (value or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        return {
            "number": args.get("number", ""),
            "client": args.get("client", ""),
            "cuit": args.get("cuit", ""),
            "date_from": _parse_date(args.get("date_from")),
            "date_to": _parse_date(args.get("date_to")),
            "doc_type": args.get("doc_type", ""),
            "status": args.get("status", ""),
            "branch": args.get("branch", ""),
            "user_id": args.get("user_id", ""),
            "amount_min": _parse_float(args.get("amount_min")),
            "amount_max": _parse_float(args.get("amount_max")),
            "payment_method": args.get("payment_method", ""),
        }

    @staticmethod
    def next_number_preview(number_value):
        raw = (number_value or "").strip()
        if "-" not in raw:
            return raw or "00001-00000001"
        left, right = raw.split("-", 1)
        try:
            current = int(right)
        except ValueError:
            return raw
        return f"{left}-{str(current + 1).zfill(len(right))}"
