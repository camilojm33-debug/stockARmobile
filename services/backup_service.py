"""Servicios de backup/restauracion por empresa para StockArmobile."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from flask import current_app


class BackupService:
    CURRENT_SCHEMA_VERSION = 2
    SUPPORTED_SCHEMA_VERSIONS = {1, 2}
    PLAN_LIMITS = {
        "entrepreneur": 1,
        "business": 2,
        "premium": 3,
        "trial": 1,
    }

    COMPANY_FIELDS = [
        "id",
        "name",
        "legal_name",
        "address",
        "province",
        "city",
        "postal_code",
        "phone",
        "whatsapp",
        "contact_email",
        "website",
        "tax_id",
        "social_facebook",
        "social_instagram",
        "social_tiktok",
        "social_youtube",
        "social_linkedin",
        "payment_alias",
        "payment_cbu",
        "payment_cvu",
        "payment_qr_text",
        "payment_qr_url",
        "logo",
        "language",
        "timezone",
        "currency",
        "date_format",
        "numbering_format",
        "printer_settings_json",
        "preferences_json",
        "schedules_json",
    ]

    COMPANY_CORE_FIELDS = [
        "name",
        "legal_name",
        "address",
        "province",
        "city",
        "postal_code",
        "phone",
        "whatsapp",
        "contact_email",
        "website",
        "tax_id",
        "social_facebook",
        "social_instagram",
        "social_tiktok",
        "social_youtube",
        "social_linkedin",
        "payment_alias",
        "payment_cbu",
        "payment_cvu",
        "payment_qr_text",
        "payment_qr_url",
        "logo",
    ]

    COMPANY_GENERAL_FIELDS = [
        "language",
        "timezone",
        "currency",
        "date_format",
        "numbering_format",
        "printer_settings_json",
        "preferences_json",
        "schedules_json",
    ]

    FULL_RESTORE_SECTIONS = [
        "company",
        "general",
        "products",
        "inventory",
        "categories",
        "clients",
        "sales",
        "employees",
        "schedules",
    ]

    SECTION_LABELS = {
        "company": "Empresa",
        "general": "Configuración general",
        "products": "Productos",
        "inventory": "Inventario",
        "categories": "Categorías",
        "clients": "Clientes",
        "sales": "Ventas",
        "employees": "Empleados",
        "schedules": "Horarios",
    }

    BACKUP_MODELS = [
        "Product",
        "Client",
        "Supplier",
        "PurchaseOrder",
        "PurchaseItem",
        "Sale",
        "SaleItem",
        "CashSession",
        "CashMovement",
        "Expense",
        "User",
    ]

    @staticmethod
    def _plan_context(company_id: int):
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        subscription = SubscriptionService.active_subscription_for_company(company_id)
        plan = getattr(subscription, "plan", None)
        if plan is None:
            plan = PlanService.get_plan(code="trial")
        plan_code = (getattr(plan, "code", None) or "trial").strip().lower()
        plan_name = getattr(plan, "name", None) or plan_code.title()
        limit = BackupService.PLAN_LIMITS.get(plan_code, 1)
        return {
            "code": plan_code,
            "name": plan_name,
            "limit": int(limit),
        }

    @staticmethod
    def _backup_root() -> Path:
        root = Path(current_app.instance_path) / "backups"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _company_dir(company_id: int) -> Path:
        path = BackupService._backup_root() / f"company_{int(company_id)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _serialize_value(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        return value

    @staticmethod
    def _system_version() -> str:
        try:
            version = current_app.config.get("APP_VERSION")
        except RuntimeError:
            version = None
        return (version or "1.0").strip()

    @staticmethod
    def _normalize_backup_payload(payload):
        if not isinstance(payload, dict):
            raise ValueError("Formato de backup invalido.")
        version = int(payload.get("schema_version") or payload.get("format_version") or 1)
        if version not in BackupService.SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError("Version de backup no compatible.")
        if int(payload.get("company_id") or 0) <= 0:
            raise ValueError("El backup no contiene company_id valido.")
        return payload

    @staticmethod
    def _load_payload_from_bytes(raw_bytes: bytes):
        if not raw_bytes:
            raise ValueError("Archivo de backup vacio.")
        try:
            decoded = gzip.decompress(raw_bytes).decode("utf-8")
        except Exception:
            try:
                decoded = raw_bytes.decode("utf-8")
            except Exception as exc:
                raise ValueError("No se pudo leer el archivo de backup.") from exc
        try:
            return BackupService._normalize_backup_payload(json.loads(decoded))
        except Exception as exc:
            raise ValueError("No se pudo leer el archivo de backup.") from exc

    @staticmethod
    def _section_counts(payload):
        products = payload.get("products") or []
        categories = {str(row.get("category") or "").strip() for row in products if str(row.get("category") or "").strip()}
        return {
            "products": len(products),
            "inventory": len(products),
            "categories": len(categories),
            "clients": len(payload.get("clients") or []),
            "sales": len(payload.get("sales") or []),
            "employees": len(payload.get("users") or []),
            "schedules": 1 if (payload.get("company") or {}).get("schedules_json") else 0,
        }

    @staticmethod
    def backup_summary_from_payload(payload):
        normalized = BackupService._normalize_backup_payload(payload)
        counts = BackupService._section_counts(normalized)
        company_payload = normalized.get("company") or {}
        return {
            "schema_version": int(normalized.get("schema_version") or 1),
            "system_version": (normalized.get("system_version") or normalized.get("app_version") or BackupService._system_version()),
            "company_id": int(normalized.get("company_id") or 0),
            "generated_at": normalized.get("generated_at"),
            "company_name": company_payload.get("name"),
            "products": counts["products"],
            "inventory": counts["inventory"],
            "categories": counts["categories"],
            "clients": counts["clients"],
            "sales": counts["sales"],
            "employees": counts["employees"],
            "schedules": counts["schedules"],
        }

    @staticmethod
    def summarize_backup(backup_log):
        payload = BackupService._load_payload(backup_log)
        summary = BackupService.backup_summary_from_payload(payload)
        summary.update({
            "backup_id": backup_log.id,
            "status": backup_log.status,
            "trigger_type": backup_log.trigger_type,
            "plan_code": backup_log.plan_code,
            "file_name": backup_log.file_name,
            "file_size_bytes": int(backup_log.file_size_bytes or 0),
            "created_at": backup_log.created_at,
            "restored_at": backup_log.restored_at,
            "detail": backup_log.detail,
            "source_company_id": int(backup_log.company_id or 0),
        })
        return summary

    @staticmethod
    def restore_section_options():
        return [
            {"key": "full", "label": "Restauración completa"},
            {"key": "products", "label": "Productos"},
            {"key": "inventory", "label": "Inventario"},
            {"key": "clients", "label": "Clientes"},
            {"key": "sales", "label": "Ventas"},
            {"key": "categories", "label": "Categorías"},
            {"key": "company", "label": "Empresa"},
            {"key": "employees", "label": "Empleados"},
            {"key": "schedules", "label": "Horarios"},
            {"key": "general", "label": "Configuración general"},
        ]

    @staticmethod
    def _normalize_sections(sections):
        requested = [str(section or "").strip().lower() for section in (sections or []) if str(section or "").strip()]
        if not requested or "full" in requested or "completa" in requested:
            return list(BackupService.FULL_RESTORE_SECTIONS)
        normalized = []
        for section in requested:
            if section in BackupService.SECTION_LABELS and section not in normalized:
                normalized.append(section)
        return normalized or list(BackupService.FULL_RESTORE_SECTIONS)

    @staticmethod
    def _row_lookup(rows):
        by_id = {}
        by_barcode = {}
        for row in rows or []:
            row_id = row.get("id")
            if row_id is not None:
                by_id[int(row_id)] = row
            barcode = str(row.get("barcode") or "").strip()
            if barcode:
                by_barcode[barcode] = row
        return by_id, by_barcode

    @staticmethod
    def _backup_file_name(company_id: int) -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        return f"backup_company_{int(company_id)}_{timestamp}.json.gz"

    @staticmethod
    def _sync_sequences(models):
        from app import db

        if db.engine.dialect.name != "postgresql":
            return
        for model in models:
            mapper = sa.inspect(model)
            if not mapper.primary_key:
                continue
            pk_column = mapper.primary_key[0]
            sequence_name = db.session.execute(
                sa.text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": model.__tablename__, "column_name": pk_column.name},
            ).scalar()
            if not sequence_name:
                continue
            max_value = db.session.execute(
                sa.text(f'SELECT COALESCE(MAX("{pk_column.name}"), 0) FROM "{model.__tablename__}"')
            ).scalar() or 0
            db.session.execute(
                sa.text("SELECT setval(:sequence_name, :max_value, true)"),
                {"sequence_name": sequence_name, "max_value": int(max_value)},
            )

    @staticmethod
    def _deserialize_for_column(column, value):
        if value is None:
            return None
        col_type = column.type
        if isinstance(col_type, sa.DateTime):
            if isinstance(value, str):
                return datetime.fromisoformat(value)
            return value
        if isinstance(col_type, sa.Date):
            if isinstance(value, str):
                return date.fromisoformat(value)
            return value
        if isinstance(col_type, (sa.Numeric, sa.DECIMAL)):
            return Decimal(str(value))
        if isinstance(col_type, sa.Integer):
            return int(value)
        if isinstance(col_type, sa.BigInteger):
            return int(value)
        if isinstance(col_type, sa.Float):
            return float(value)
        if isinstance(col_type, sa.Boolean):
            return bool(value)
        return value

    @staticmethod
    def _row_to_dict(row, allowed_fields=None):
        mapper = sa.inspect(row.__class__)
        payload = {}
        allowed = set(allowed_fields or []) if allowed_fields else None
        for column in mapper.columns:
            name = column.name
            if allowed is not None and name not in allowed:
                continue
            payload[name] = BackupService._serialize_value(getattr(row, name))
        return payload

    @staticmethod
    def _payload_for_company(company_id: int):
        from app import (
            CashMovement,
            CashSession,
            Client,
            Company,
            Expense,
            Product,
            PurchaseItem,
            PurchaseOrder,
            Sale,
            SaleItem,
            Supplier,
            User,
        )

        company = Company.query.filter_by(id=company_id).first_or_404()

        users = (
            User.query.filter(
                User.company_id == company_id,
                User.role != "superadmin",
            )
            .order_by(User.id.asc())
            .all()
        )
        products = Product.query.filter_by(company_id=company_id).order_by(Product.id.asc()).all()
        clients = Client.query.filter_by(company_id=company_id).order_by(Client.id.asc()).all()
        suppliers = Supplier.query.filter_by(company_id=company_id).order_by(Supplier.id.asc()).all()

        purchase_orders = PurchaseOrder.query.filter_by(company_id=company_id).order_by(PurchaseOrder.id.asc()).all()
        purchase_order_ids = [row.id for row in purchase_orders]
        purchase_items = []
        if purchase_order_ids:
            purchase_items = (
                PurchaseItem.query.filter(PurchaseItem.purchase_order_id.in_(purchase_order_ids))
                .order_by(PurchaseItem.id.asc())
                .all()
            )

        sales = Sale.query.filter_by(company_id=company_id).order_by(Sale.id.asc()).all()
        sale_ids = [row.id for row in sales]
        sale_items = []
        if sale_ids:
            sale_items = (
                SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids))
                .order_by(SaleItem.id.asc())
                .all()
            )

        cash_sessions = CashSession.query.filter_by(company_id=company_id).order_by(CashSession.id.asc()).all()
        cash_movements = CashMovement.query.filter_by(company_id=company_id).order_by(CashMovement.id.asc()).all()
        expenses = Expense.query.filter_by(company_id=company_id).order_by(Expense.id.asc()).all()
        categories = sorted({(product.category or "").strip() for product in products if (product.category or "").strip()})
        base_payload = {
            "company": BackupService._row_to_dict(company, allowed_fields=BackupService.COMPANY_FIELDS),
            "users": [BackupService._row_to_dict(row) for row in users],
            "products": [BackupService._row_to_dict(row) for row in products],
            "clients": [BackupService._row_to_dict(row) for row in clients],
            "sales": [BackupService._row_to_dict(row) for row in sales],
        }

        return {
            "schema_version": BackupService.CURRENT_SCHEMA_VERSION,
            "system_version": BackupService._system_version(),
            "generated_at": datetime.utcnow().isoformat(),
            "company_id": company_id,
            "company": base_payload["company"],
            "users": base_payload["users"],
            "products": base_payload["products"],
            "categories": categories,
            "clients": [BackupService._row_to_dict(row) for row in clients],
            "suppliers": [BackupService._row_to_dict(row) for row in suppliers],
            "purchase_orders": [BackupService._row_to_dict(row) for row in purchase_orders],
            "purchase_items": [BackupService._row_to_dict(row) for row in purchase_items],
            "sales": [BackupService._row_to_dict(row) for row in sales],
            "sale_items": [BackupService._row_to_dict(row) for row in sale_items],
            "cash_sessions": [BackupService._row_to_dict(row) for row in cash_sessions],
            "cash_movements": [BackupService._row_to_dict(row) for row in cash_movements],
            "expenses": [BackupService._row_to_dict(row) for row in expenses],
            "section_counts": BackupService._section_counts(base_payload | {"categories": categories}),
        }

    @staticmethod
    def _deserialize_rows(model, rows):
        mapper = sa.inspect(model)
        columns = {column.name: column for column in mapper.columns}
        result = []
        for row in rows:
            payload = {}
            for key, value in row.items():
                column = columns.get(key)
                if column is None:
                    continue
                payload[key] = BackupService._deserialize_for_column(column, value)
            result.append(payload)
        return result

    @staticmethod
    def plan_limit_status(company_id: int):
        from app import BackupLog

        plan = BackupService._plan_context(company_id)
        active_count = BackupLog.query.filter(BackupLog.company_id == company_id, BackupLog.status.in_(["ready", "restored"])).count()
        return {
            "plan_code": plan["code"],
            "plan_name": plan["name"],
            "limit": plan["limit"],
            "count": active_count,
            "remaining": max(plan["limit"] - active_count, 0),
        }

    @staticmethod
    def create_manual_backup(company_id: int, *, user_id: int | None, trigger_type: str = "manual"):
        from app import BackupLog, db

        plan = BackupService._plan_context(company_id)
        file_name = BackupService._backup_file_name(company_id)
        backup_dir = BackupService._company_dir(company_id)
        backup_path = backup_dir / file_name

        payload = BackupService._payload_for_company(company_id)
        with gzip.open(backup_path, "wt", encoding="utf-8") as gz_file:
            json.dump(payload, gz_file, ensure_ascii=False)

        size_bytes = backup_path.stat().st_size if backup_path.exists() else 0
        backup = BackupLog(
            company_id=company_id,
            status="ready",
            trigger_type=trigger_type,
            plan_code=plan["code"],
            file_name=file_name,
            file_size_bytes=size_bytes,
            path=str(backup_path),
            created_by_user_id=user_id,
            is_automated=(trigger_type != "manual"),
            metadata_json=json.dumps({"company_id": company_id, "plan": plan["code"], "schema_version": payload["schema_version"], "system_version": payload["system_version"]}),
            detail="Backup creado correctamente.",
        )
        db.session.add(backup)
        db.session.flush()

        # Limite por plan: elimina automaticamente el mas antiguo.
        backups = (
            BackupLog.query.filter_by(company_id=company_id)
            .order_by(BackupLog.created_at.desc(), BackupLog.id.desc())
            .all()
        )
        if len(backups) > plan["limit"]:
            for stale in backups[plan["limit"] :]:
                try:
                    stale_path = Path(stale.path or "")
                    if stale_path.exists():
                        stale_path.unlink()
                except Exception:
                    current_app.logger.warning("No se pudo eliminar archivo de backup antiguo id=%s", stale.id)
                db.session.delete(stale)

        return backup, plan

    @staticmethod
    def import_backup_file(*, company_id: int, file_storage, created_by_user_id: int | None, trigger_type: str = "imported"):
        from app import BackupLog, db

        raw_bytes = file_storage.read()
        payload = BackupService._load_payload_from_bytes(raw_bytes)
        if int(payload.get("company_id") or 0) != int(company_id):
            raise ValueError("El backup no corresponde a la empresa seleccionada.")

        plan = BackupService._plan_context(company_id)
        backup_dir = BackupService._company_dir(company_id)
        file_name = BackupService._backup_file_name(company_id)
        backup_path = backup_dir / file_name
        with gzip.open(backup_path, "wt", encoding="utf-8") as gz_file:
            json.dump(payload, gz_file, ensure_ascii=False)

        backup = BackupLog(
            company_id=company_id,
            status="ready",
            trigger_type=trigger_type,
            plan_code=plan["code"],
            file_name=file_name,
            file_size_bytes=backup_path.stat().st_size if backup_path.exists() else len(raw_bytes),
            path=str(backup_path),
            created_by_user_id=created_by_user_id,
            is_automated=False,
            metadata_json=json.dumps({
                "company_id": company_id,
                "plan": plan["code"],
                "schema_version": payload.get("schema_version", 1),
                "system_version": payload.get("system_version", BackupService._system_version()),
                "imported_from": getattr(file_storage, "filename", None),
            }),
            detail="Backup importado correctamente.",
        )
        db.session.add(backup)
        db.session.flush()

        backups = (
            BackupLog.query.filter_by(company_id=company_id)
            .order_by(BackupLog.created_at.desc(), BackupLog.id.desc())
            .all()
        )
        if len(backups) > plan["limit"]:
            for stale in backups[plan["limit"] :]:
                try:
                    stale_path = Path(stale.path or "")
                    if stale_path.exists():
                        stale_path.unlink()
                except Exception:
                    current_app.logger.warning("No se pudo eliminar archivo de backup antiguo id=%s", stale.id)
                db.session.delete(stale)

        return backup, plan, payload

    @staticmethod
    def _load_payload(backup_log):
        backup_path = Path(backup_log.path or "")
        if not backup_path.exists():
            raise FileNotFoundError("No existe el archivo de backup.")
        with backup_path.open("rb") as file_handle:
            return BackupService._load_payload_from_bytes(file_handle.read())

    @staticmethod
    def _restore_product_subset(current_products, backup_rows, fields):
        backup_by_id, backup_by_barcode = BackupService._row_lookup(backup_rows)
        for product in current_products:
            payload = backup_by_id.get(int(product.id)) or backup_by_barcode.get(str(product.barcode or "").strip())
            if not payload:
                continue
            mapper = sa.inspect(product.__class__)
            for field in fields:
                if field in payload and field in mapper.columns:
                    setattr(product, field, BackupService._deserialize_for_column(mapper.columns[field], payload.get(field)))

    @staticmethod
    def _restore_full_company_dataset(db_session, model, rows):
        model.query.filter_by(company_id=rows[0].get("company_id") if rows else None).delete(synchronize_session=False)
        if rows:
            db_session.execute(model.__table__.insert(), BackupService._deserialize_rows(model, rows))

    @staticmethod
    def restore_backup(backup_log, *, expected_company_id: int, restored_by_user_id: int | None, sections=None):
        from app import (
            CashMovement,
            CashSession,
            Client,
            Company,
            Expense,
            Product,
            PurchaseItem,
            PurchaseOrder,
            Sale,
            SaleItem,
            Supplier,
            User,
            db,
            utcnow,
        )

        if int(backup_log.company_id or 0) != int(expected_company_id):
            raise PermissionError("Backup fuera de alcance para la empresa solicitada.")

        payload = BackupService._load_payload(backup_log)
        if int(payload.get("company_id") or 0) != int(expected_company_id):
            raise ValueError("El backup no corresponde a la empresa seleccionada.")

        sections = BackupService._normalize_sections(sections)

        company = Company.query.filter_by(id=expected_company_id).first_or_404()
        company_payload = payload.get("company") or {}

        if "company" in sections:
            for field in BackupService.COMPANY_CORE_FIELDS:
                if field in company_payload:
                    setattr(company, field, company_payload.get(field))
        if "general" in sections or "schedules" in sections:
            for field in BackupService.COMPANY_GENERAL_FIELDS:
                if field in company_payload:
                    setattr(company, field, company_payload.get(field))

        current_products = Product.query.filter_by(company_id=expected_company_id).order_by(Product.id.asc()).all()
        if "products" in sections:
            Product.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            product_rows = BackupService._deserialize_rows(Product, payload.get("products") or [])
            if product_rows:
                db.session.execute(Product.__table__.insert(), product_rows)
            current_products = Product.query.filter_by(company_id=expected_company_id).order_by(Product.id.asc()).all()
        else:
            if "inventory" in sections:
                BackupService._restore_product_subset(current_products, payload.get("products") or [], ["stock", "min_stock"])
            if "categories" in sections:
                BackupService._restore_product_subset(current_products, payload.get("products") or [], ["category"])

        if "clients" in sections:
            Client.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            client_rows = BackupService._deserialize_rows(Client, payload.get("clients") or [])
            if client_rows:
                db.session.execute(Client.__table__.insert(), client_rows)

        if "sales" in sections:
            sales_ids = [row.id for row in Sale.query.filter_by(company_id=expected_company_id).all()]
            if sales_ids:
                SaleItem.query.filter(SaleItem.sale_id.in_(sales_ids)).delete(synchronize_session=False)
            Sale.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            sale_rows = BackupService._deserialize_rows(Sale, payload.get("sales") or [])
            sale_item_rows = BackupService._deserialize_rows(SaleItem, payload.get("sale_items") or [])
            if sale_rows:
                db.session.execute(Sale.__table__.insert(), sale_rows)
            if sale_item_rows:
                db.session.execute(SaleItem.__table__.insert(), sale_item_rows)

        if "employees" in sections:
            User.query.filter(User.company_id == expected_company_id, User.role != "superadmin").delete(synchronize_session=False)
            user_rows = BackupService._deserialize_rows(User, payload.get("users") or [])
            if user_rows:
                db.session.execute(User.__table__.insert(), user_rows)

        if sections == BackupService.FULL_RESTORE_SECTIONS or set(sections) == set(BackupService.FULL_RESTORE_SECTIONS):
            purchase_order_ids = [row.id for row in PurchaseOrder.query.filter_by(company_id=expected_company_id).all()]
            if purchase_order_ids:
                PurchaseItem.query.filter(PurchaseItem.purchase_order_id.in_(purchase_order_ids)).delete(synchronize_session=False)
            PurchaseOrder.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            purchase_order_rows = BackupService._deserialize_rows(PurchaseOrder, payload.get("purchase_orders") or [])
            purchase_item_rows = BackupService._deserialize_rows(PurchaseItem, payload.get("purchase_items") or [])
            if purchase_order_rows:
                db.session.execute(PurchaseOrder.__table__.insert(), purchase_order_rows)
            if purchase_item_rows:
                db.session.execute(PurchaseItem.__table__.insert(), purchase_item_rows)

            CashMovement.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            CashSession.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            Expense.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            Supplier.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
            cash_session_rows = BackupService._deserialize_rows(CashSession, payload.get("cash_sessions") or [])
            cash_movement_rows = BackupService._deserialize_rows(CashMovement, payload.get("cash_movements") or [])
            expense_rows = BackupService._deserialize_rows(Expense, payload.get("expenses") or [])
            supplier_rows = BackupService._deserialize_rows(Supplier, payload.get("suppliers") or [])
            if cash_session_rows:
                db.session.execute(CashSession.__table__.insert(), cash_session_rows)
            if cash_movement_rows:
                db.session.execute(CashMovement.__table__.insert(), cash_movement_rows)
            if expense_rows:
                db.session.execute(Expense.__table__.insert(), expense_rows)
            if supplier_rows:
                db.session.execute(Supplier.__table__.insert(), supplier_rows)

        BackupService._sync_sequences([Product, Client, Supplier, PurchaseOrder, PurchaseItem, Sale, SaleItem, CashSession, CashMovement, Expense, User])

        backup_log.restored_at = utcnow()
        backup_log.restored_by_user_id = restored_by_user_id
        backup_log.status = "restored"
        backup_log.detail = "Backup restaurado correctamente."
        return backup_log

    @staticmethod
    def backup_download_path(backup_log) -> Path:
        path = Path(backup_log.path or "")
        if not path.exists():
            raise FileNotFoundError("No existe el archivo de backup.")
        return path

    @staticmethod
    def delete_backup(backup_log):
        from app import db

        backup_path = Path(backup_log.path or "")
        if backup_path.exists():
            backup_path.unlink()
        db.session.delete(backup_log)

    @staticmethod
    def company_backups(company_id: int):
        from app import BackupLog

        return (
            BackupLog.query.filter_by(company_id=company_id)
            .order_by(BackupLog.created_at.desc(), BackupLog.id.desc())
            .all()
        )

    @staticmethod
    def superadmin_backups(*, q="", company_id=None, status="all", plan_code="all"):
        from app import BackupLog, Company, User, db

        query = BackupLog.query.join(Company, Company.id == BackupLog.company_id).outerjoin(User, User.id == BackupLog.created_by_user_id)
        text_q = (q or "").strip()
        if text_q:
            like = f"%{text_q}%"
            query = query.filter(
                db.or_(
                    Company.name.ilike(like),
                    Company.contact_email.ilike(like),
                    User.username.ilike(like),
                    BackupLog.file_name.ilike(like),
                )
            )
        if company_id:
            query = query.filter(BackupLog.company_id == int(company_id))
        normalized_status = (status or "all").strip().lower()
        if normalized_status != "all":
            query = query.filter(BackupLog.status == normalized_status)
        normalized_plan = (plan_code or "all").strip().lower()
        if normalized_plan != "all":
            query = query.filter(BackupLog.plan_code == normalized_plan)

        return query.order_by(BackupLog.created_at.desc(), BackupLog.id.desc()).all()

    @staticmethod
    def automation_scaffold():
        # Punto unico de extension para jobs automaticos (diario/semanal/mensual).
        return {
            "enabled": False,
            "strategies": ["daily", "weekly", "monthly"],
            "note": "Estructura lista para scheduler externo sin cambiar arquitectura actual.",
        }
