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

        return {
            "schema_version": 1,
            "generated_at": datetime.utcnow().isoformat(),
            "company_id": company_id,
            "company": BackupService._row_to_dict(company, allowed_fields=BackupService.COMPANY_FIELDS),
            "users": [BackupService._row_to_dict(row) for row in users],
            "products": [BackupService._row_to_dict(row) for row in products],
            "clients": [BackupService._row_to_dict(row) for row in clients],
            "suppliers": [BackupService._row_to_dict(row) for row in suppliers],
            "purchase_orders": [BackupService._row_to_dict(row) for row in purchase_orders],
            "purchase_items": [BackupService._row_to_dict(row) for row in purchase_items],
            "sales": [BackupService._row_to_dict(row) for row in sales],
            "sale_items": [BackupService._row_to_dict(row) for row in sale_items],
            "cash_sessions": [BackupService._row_to_dict(row) for row in cash_sessions],
            "cash_movements": [BackupService._row_to_dict(row) for row in cash_movements],
            "expenses": [BackupService._row_to_dict(row) for row in expenses],
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
        active_count = BackupLog.query.filter_by(company_id=company_id, status="ready").count()
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
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        file_name = f"backup_company_{company_id}_{timestamp}.json.gz"
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
            metadata_json=json.dumps({"company_id": company_id, "plan": plan["code"]}),
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
    def _load_payload(backup_log):
        backup_path = Path(backup_log.path or "")
        if not backup_path.exists():
            raise FileNotFoundError("No existe el archivo de backup.")
        with gzip.open(backup_path, "rt", encoding="utf-8") as gz_file:
            return json.load(gz_file)

    @staticmethod
    def restore_backup(backup_log, *, expected_company_id: int, restored_by_user_id: int | None):
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

        company = Company.query.filter_by(id=expected_company_id).first_or_404()

        # 1) Restaurar datos de empresa (solo campos permitidos de configuracion interna).
        company_payload = payload.get("company") or {}
        for field in BackupService.COMPANY_FIELDS:
            if field in {"id"}:
                continue
            if field in company_payload:
                setattr(company, field, company_payload.get(field))

        # 2) Limpiar datos company-scoped con orden seguro.
        sales_ids = [row.id for row in Sale.query.filter_by(company_id=expected_company_id).all()]
        if sales_ids:
            SaleItem.query.filter(SaleItem.sale_id.in_(sales_ids)).delete(synchronize_session=False)
        Sale.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)

        purchase_order_ids = [row.id for row in PurchaseOrder.query.filter_by(company_id=expected_company_id).all()]
        if purchase_order_ids:
            PurchaseItem.query.filter(PurchaseItem.purchase_order_id.in_(purchase_order_ids)).delete(synchronize_session=False)
        PurchaseOrder.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)

        CashMovement.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
        CashSession.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
        Expense.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
        Product.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
        Client.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)
        Supplier.query.filter_by(company_id=expected_company_id).delete(synchronize_session=False)

        # Usuarios de empresa (sin superadmin): se restauran por completo.
        User.query.filter(User.company_id == expected_company_id, User.role != "superadmin").delete(synchronize_session=False)

        # 3) Reinsertar dataset.
        db.session.execute(User.__table__.insert(), BackupService._deserialize_rows(User, payload.get("users") or []))
        db.session.execute(Product.__table__.insert(), BackupService._deserialize_rows(Product, payload.get("products") or []))
        db.session.execute(Client.__table__.insert(), BackupService._deserialize_rows(Client, payload.get("clients") or []))
        db.session.execute(Supplier.__table__.insert(), BackupService._deserialize_rows(Supplier, payload.get("suppliers") or []))
        db.session.execute(PurchaseOrder.__table__.insert(), BackupService._deserialize_rows(PurchaseOrder, payload.get("purchase_orders") or []))
        db.session.execute(PurchaseItem.__table__.insert(), BackupService._deserialize_rows(PurchaseItem, payload.get("purchase_items") or []))
        db.session.execute(Sale.__table__.insert(), BackupService._deserialize_rows(Sale, payload.get("sales") or []))
        db.session.execute(SaleItem.__table__.insert(), BackupService._deserialize_rows(SaleItem, payload.get("sale_items") or []))
        db.session.execute(CashSession.__table__.insert(), BackupService._deserialize_rows(CashSession, payload.get("cash_sessions") or []))
        db.session.execute(CashMovement.__table__.insert(), BackupService._deserialize_rows(CashMovement, payload.get("cash_movements") or []))
        db.session.execute(Expense.__table__.insert(), BackupService._deserialize_rows(Expense, payload.get("expenses") or []))

        backup_log.restored_at = utcnow()
        backup_log.restored_by_user_id = restored_by_user_id
        backup_log.status = "restored"
        backup_log.detail = "Backup restaurado correctamente."

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
