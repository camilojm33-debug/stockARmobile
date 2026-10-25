import os
from datetime import date
from decimal import Decimal

from app import (
    CashMovement, CashSession, Client, Company, Expense, Product, PurchaseItem,
    PurchaseOrder, Quote, Sale, SaleItem, Supplier, User, db,
)
from services.backup_service import BackupService
from stockarmobile.supplier_purchase_payments import PurchasePaymentDetail
from pricing_controller import PriceControllerBatch, PriceControllerItem
from promotions import Promotion


def test_backup_restore_round_trip_rolls_back_changes(app, monkeypatch):
    monkeypatch.setenv("BACKUP_UPLOAD_DIR", str((__import__("pathlib").Path(app.instance_path) / "qa-backups")))
    sequence_sync_calls = []
    monkeypatch.setattr(
        BackupService,
        "_sync_sequences",
        staticmethod(lambda models: sequence_sync_calls.append(tuple(model.__name__ for model in models))),
    )
    with app.app_context():
        company = Company(name="Backup QA", active=True)
        db.session.add(company)
        db.session.flush()

        db.session.add(
            Product(
                company_id=company.id,
                barcode="BKP-001",
                name="Producto QA",
                price=123.45,
                stock=7,
                active=True,
            )
        )
        db.session.add(
            Client(
                company_id=company.id,
                name="Cliente QA",
                phone="123",
            )
        )
        db.session.commit()

        backup, _plan = BackupService.create_manual_backup(company.id, user_id=None)
        db.session.commit()

        result = BackupService.verify_restore_round_trip(
            backup,
            expected_company_id=company.id,
        )
        assert result["valid"] is True
        assert result["rolled_back"] is True
        assert result["counts_before"]["products"] == 1
        assert result["counts_before"]["clients"] == 1
        assert backup.status == "ready"
        assert sequence_sync_calls == [], "Verification must not mutate PostgreSQL sequences outside its rollbackable savepoint."


def test_backup_maintenance_endpoint_requires_token(app, monkeypatch):
    monkeypatch.delenv("BACKUP_AUTOMATION_TOKEN", raising=False)
    client = app.test_client()
    response = client.post("/internal/maintenance/backups/run")
    assert response.status_code == 403


def test_backup_maintenance_endpoint_accepts_valid_token_and_processes_companies(app, monkeypatch):
    monkeypatch.setenv("BACKUP_AUTOMATION_TOKEN", "test-token")
    company = None
    with app.app_context():
        company = Company(name="Maintenance QA", active=True)
        db.session.add(company)
        db.session.commit()
        company_id = company.id

    calls = []

    def fake_backup(company_id, *, user_id, trigger_type):
        class FakeBackup:
            id = 123
            file_name = "backup.qa.gz"

        calls.append((company_id, user_id, trigger_type))
        return FakeBackup(), {"code": "trial"}

    monkeypatch.setattr(
        BackupService,
        "automated_backup_due",
        staticmethod(lambda company_id, *, day_of_month=None: True),
    )
    monkeypatch.setattr(BackupService, "create_manual_backup", staticmethod(fake_backup))
    client = app.test_client()
    response = client.post(
        "/internal/maintenance/backups/run",
        headers={"X-Backup-Automation-Token": "test-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls
    assert calls[0][0] == company_id
    assert calls[0][1] is None
    assert calls[0][2] == "automated_render"


def test_gmail_watch_maintenance_requires_token(app, monkeypatch):
    monkeypatch.delenv("GMAIL_WATCH_AUTOMATION_TOKEN", raising=False)
    client = app.test_client()
    response = client.post("/internal/maintenance/gmail/watch")
    assert response.status_code == 403


def test_gmail_watch_maintenance_renews_with_valid_token(app, monkeypatch):
    monkeypatch.setenv("GMAIL_WATCH_AUTOMATION_TOKEN", "gmail-test-token")
    calls = []

    def fake_renew_watch():
        calls.append(True)
        return {"status": "renewed", "history_id": "123", "expiration": "456"}

    monkeypatch.setattr(
        "services.gmail_commercial_service.renew_watch",
        fake_renew_watch,
    )
    client = app.test_client()
    response = client.post(
        "/internal/maintenance/gmail/watch",
        headers={"X-Gmail-Watch-Automation-Token": "gmail-test-token"},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [True]

    
def test_backup_monthly_slots_match_subscription_tiers():
    assert BackupService.monthly_backup_slots("trial") == (1,)
    assert BackupService.monthly_backup_slots("entrepreneur") == (1,)
    assert BackupService.monthly_backup_slots("business") == (1, 15)
    assert BackupService.monthly_backup_slots("premium") == (1, 10, 20)


def test_backup_due_only_on_plan_monthly_slots(app, monkeypatch):
    with app.app_context():
        company = Company(name="Schedule QA", active=True)
        db.session.add(company)
        db.session.commit()

        monkeypatch.setattr(
            BackupService,
            "_plan_context",
            staticmethod(lambda company_id: {"code": "business", "name": "Negocio", "limit": 2}),
        )

        assert BackupService.automated_backup_due(company.id, day_of_month=1) is True
        assert BackupService.automated_backup_due(company.id, day_of_month=15) is True
        assert BackupService.automated_backup_due(company.id, day_of_month=10) is False



def test_full_backup_restore_round_trip_preserves_sales_purchase_and_cash_relations(app, monkeypatch):
    monkeypatch.setenv("BACKUP_UPLOAD_DIR", str((__import__("pathlib").Path(app.instance_path) / "qa-backups-fk")))
    with app.app_context():
        company = Company(name="Backup FK QA", active=True)
        db.session.add(company)
        db.session.flush()

        user = User(
            username="backup_fk_user",
            email="backup_fk_user@example.test",
            password_hash="unused-test-hash",
            role="admin",
            active=True,
            company_id=company.id,
        )
        supplier = Supplier(name="Proveedor FK QA", company_id=company.id, active=True)
        client = Client(name="Cliente FK QA", phone="555", company_id=company.id)
        db.session.add_all([user, supplier, client])
        db.session.flush()

        product = Product(
            company_id=company.id,
            supplier_id=supplier.id,
            barcode="BKP-FK-001",
            name="Producto FK QA",
            price=120,
            cost_price=40,
            stock=5,
            active=True,
        )
        db.session.add(product)
        db.session.flush()

        promotion = Promotion(
            company_id=company.id,
            product_id=product.id,
            name="Promoción FK QA",
            type="percentage",
            created_by_user_id=user.id,
        )
        price_batch = PriceControllerBatch(
            company_id=company.id,
            user_id=user.id,
            adjustment_type="percent",
            direction="increase",
            adjustment_value=Decimal("5.0"),
            name="Lote FK QA",
        )
        db.session.add_all([promotion, price_batch])
        db.session.flush()
        price_item = PriceControllerItem(
            batch_id=price_batch.id,
            product_id=product.id,
            old_price=Decimal("120.00"),
            new_price=Decimal("126.00"),
        )
        db.session.add(price_item)

        cash_session = CashSession(
            user_id=user.id,
            opened_by_user_id=user.id,
            company_id=company.id,
            opening_amount=100,
            status="abierta",
        )
        db.session.add(cash_session)
        db.session.flush()

        purchase = PurchaseOrder(
            company_id=company.id,
            supplier_id=supplier.id,
            status="recibida",
            subtotal=40,
            total_amount=40,
        )
        db.session.add(purchase)
        db.session.flush()
        db.session.add(PurchaseItem(
            purchase_order_id=purchase.id,
            product_id=product.id,
            quantity=1,
            unit_cost=40,
        ))
        db.session.add(PurchasePaymentDetail(
            purchase_order_id=purchase.id,
            company_id=company.id,
            payment_method="TRANSFERENCIA",
            paid_amount=Decimal("15.00"),
            secondary_paid_amount=Decimal("0.00"),
            payment_term_days=30,
            due_date=date(2026, 11, 23),
            payment_status="parcial",
            payment_reference="QA-TRANSFER-001",
            notes="Pago parcial de prueba",
        ))

        sale = Sale(
            company_id=company.id,
            client_id=client.id,
            cash_session_id=cash_session.id,
            seller_id=user.id,
            customer=client.name,
            subtotal=120,
            total_amount=120,
            paid_amount=120,
            status="confirmada",
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(
            sale_id=sale.id,
            product_id=product.id,
            quantity=1,
            price=120,
            cost_price=40,
        ))
        quote_before_snapshot = Quote(
            company_id=company.id,
            number="BKP-FK-QUOTE-001",
            client_id=client.id,
            created_by_user_id=user.id,
            seller_id=user.id,
            converted_sale_id=sale.id,
            subtotal=120,
            total_amount=120,
            status="CONVERTIDO",
        )
        db.session.add(quote_before_snapshot)
        db.session.add(CashMovement(
            session_id=cash_session.id,
            sale_id=sale.id,
            user_id=user.id,
            company_id=company.id,
            movement_type="ingreso",
            category="venta",
            amount=120,
            description="Venta vinculada",
        ))
        db.session.add(Expense(
            category="Otros",
            description="Gasto FK QA",
            amount=10,
            payment_method="EFECTIVO",
            user_id=user.id,
            company_id=company.id,
        ))
        db.session.commit()

        backup, _plan = BackupService.create_manual_backup(company.id, user_id=user.id)
        db.session.commit()

        # A newer product not present in the backup must be archived, not deleted:
        # retained catalog history may still reference products outside the snapshot.
        extra_product = Product(
            company_id=company.id,
            barcode="BKP-FK-EXTRA",
            name="Producto posterior al backup",
            price=99,
            stock=9,
            active=True,
        )
        db.session.add(extra_product)
        extra_cash_session = CashSession(
            user_id=user.id,
            opened_by_user_id=user.id,
            company_id=company.id,
            opening_amount=0,
            status="abierta",
            note="Sesión posterior al backup",
        )
        db.session.add(extra_cash_session)
        db.session.flush()
        extra_client = Client(
            name="Cliente posterior al backup",
            phone="556",
            company_id=company.id,
            active=True,
        )
        db.session.add(extra_client)
        db.session.flush()
        extra_sale = Sale(
            company_id=company.id,
            client_id=extra_client.id,
            customer=extra_client.name,
            subtotal=33,
            total_amount=33,
            paid_amount=33,
            seller_id=user.id,
            cash_session_id=extra_cash_session.id,
            status="confirmada",
        )
        db.session.add(extra_sale)
        db.session.flush()
        extra_movement = CashMovement(
            session_id=extra_cash_session.id,
            sale_id=extra_sale.id,
            user_id=user.id,
            company_id=company.id,
            movement_type="ingreso",
            category="venta",
            amount=33,
            description="Movimiento posterior al backup",
        )
        extra_expense = Expense(
            category="Otros",
            description="Gasto posterior al backup",
            amount=7,
            payment_method="EFECTIVO",
            user_id=user.id,
            company_id=company.id,
        )
        db.session.add_all([extra_movement, extra_expense])
        extra_quote = Quote(
            company_id=company.id,
            number="BKP-FK-QUOTE-POST",
            client_id=extra_client.id,
            created_by_user_id=user.id,
            seller_id=user.id,
            converted_sale_id=extra_sale.id,
            subtotal=33,
            total_amount=33,
            status="CONVERTIDO",
        )
        db.session.add(extra_quote)
        db.session.commit()
        extra_product_id = extra_product.id
        extra_client_id = extra_client.id
        extra_sale_id = extra_sale.id
        extra_quote_id = extra_quote.id
        extra_cash_session_id = extra_cash_session.id
        extra_movement_id = extra_movement.id
        extra_expense_id = extra_expense.id
        product_id = product.id
        client_id = client.id
        sale_id = sale.id
        price_batch_id = price_batch.id

        result = BackupService.verify_restore_round_trip(
            backup,
            expected_company_id=company.id,
        )
        assert result["valid"] is True
        assert result["rolled_back"] is True
        assert result["counts_after_restore"]["sales"] >= 1
        assert result["counts_after_restore"]["sale_items"] >= 1
        assert result["counts_after_restore"]["purchase_orders"] == 1
        assert result["counts_after_restore"]["purchase_items"] == 1
        assert result["counts_after_restore"]["purchase_payment_details"] == 1
        assert result["counts_after_restore"]["cash_sessions"] == 1
        assert result["counts_after_restore"]["cash_movements"] >= 1
        assert result["counts_after_restore"]["expenses"] >= 1

        # Exercise a real restore in a savepoint so the FK guarantee and archive
        # policy are observable without persisting QA data.
        savepoint = db.session.begin_nested()
        try:
            BackupService.restore_backup(
                backup,
                expected_company_id=company.id,
                restored_by_user_id=None,
                sections=list(BackupService.FULL_RESTORE_SECTIONS),
            )
            db.session.flush()
            restored_product = Product.query.filter_by(id=product_id, company_id=company.id).one()
            assert restored_product.active is True
            assert restored_product.barcode == "BKP-FK-001"
            archived_extra = Product.query.filter_by(id=extra_product_id, company_id=company.id).one()
            assert archived_extra.active is False
            assert archived_extra.stock == 0
            assert Promotion.query.filter_by(
                company_id=company.id, product_id=product_id
            ).count() == 1
            assert PriceControllerItem.query.filter_by(
                batch_id=price_batch_id, product_id=product_id
            ).count() == 1
            # Parent rows outside the snapshot are preserved, not deleted by a
            # point-in-time restore, so their dependent quotes remain intact.
            assert Client.query.filter_by(id=extra_client_id, company_id=company.id).one().active is False
            assert Sale.query.filter_by(id=extra_sale_id, company_id=company.id).count() == 1
            preserved_quote = db.session.get(Quote, extra_quote_id)
            assert preserved_quote is not None
            assert preserved_quote.client_id == extra_client_id
            assert preserved_quote.converted_sale_id == extra_sale_id
            original_quote = db.session.get(Quote, quote_before_snapshot.id)
            assert original_quote is not None
            assert original_quote.client_id == client_id
            assert original_quote.converted_sale_id == sale_id
            assert CashSession.query.filter_by(
                id=extra_cash_session_id, company_id=company.id
            ).count() == 1
            assert CashMovement.query.filter_by(
                id=extra_movement_id, company_id=company.id, sale_id=extra_sale_id
            ).count() == 1
            assert Expense.query.filter_by(
                id=extra_expense_id, company_id=company.id
            ).count() == 1
        finally:
            if savepoint.is_active:
                savepoint.rollback()
            db.session.expire_all()


def test_backup_creation_refuses_payload_over_import_size_limit(app, monkeypatch, tmp_path):
    import pytest
    from app import BackupLog

    monkeypatch.setenv("BACKUP_UPLOAD_DIR", str(tmp_path / "backup-size-qa"))
    monkeypatch.setitem(app.config, "BACKUP_MAX_UNCOMPRESSED_BYTES", 128)
    with app.app_context():
        company = Company(name="Backup size QA", active=True)
        db.session.add(company)
        db.session.commit()
        company_id = company.id
        monkeypatch.setattr(
            BackupService,
            "_payload_for_company",
            staticmethod(lambda _company_id: {
                "schema_version": 2,
                "system_version": "qa",
                "company_id": company_id,
                "payload": "x" * 500,
            }),
        )

        with pytest.raises(ValueError, match="sin comprimir"):
            BackupService.create_manual_backup(company_id, user_id=None)

        db.session.rollback()
        assert BackupLog.query.filter_by(company_id=company_id).count() == 0
        files_left = list((tmp_path / "backup-size-qa").rglob("*"))
        assert not [path for path in files_left if path.is_file()]


def test_backup_creation_refuses_compressed_payload_over_limit(app, monkeypatch):
    import pytest

    monkeypatch.setitem(app.config, "BACKUP_MAX_UNCOMPRESSED_BYTES", 1024 * 1024)
    monkeypatch.setitem(app.config, "BACKUP_MAX_COMPRESSED_BYTES", 64)
    with app.app_context():
        with pytest.raises(ValueError, match="comprimido"):
            BackupService._encode_backup_payload({
                "schema_version": 2,
                "system_version": "qa",
                "company_id": 1,
                "records": list(range(1000)),
            })



def test_postgres_backup_sequence_reset_handles_empty_and_populated_tables():
    from services.backup_service import BackupService

    assert BackupService._sequence_reset_values(0) == (1, False)
    assert BackupService._sequence_reset_values(None) == (1, False)
    assert BackupService._sequence_reset_values(17) == (17, True)


def test_full_restore_refuses_legacy_backup_when_supplier_payment_details_exist(app, monkeypatch, tmp_path):
    import pytest
    from pathlib import Path

    monkeypatch.setenv("BACKUP_UPLOAD_DIR", str(tmp_path / "qa-legacy-payment-backups"))
    with app.app_context():
        company = Company(name="Legacy payment guard QA", active=True)
        db.session.add(company)
        db.session.flush()
        supplier = Supplier(name="Proveedor QA", company_id=company.id, active=True)
        db.session.add(supplier)
        db.session.flush()
        purchase = PurchaseOrder(
            company_id=company.id,
            supplier_id=supplier.id,
            status="recibida",
            subtotal=80,
            total_amount=80,
        )
        db.session.add(purchase)
        db.session.flush()
        detail = PurchasePaymentDetail(
            purchase_order_id=purchase.id,
            company_id=company.id,
            payment_method="TRANSFERENCIA",
            paid_amount=Decimal("25.00"),
            secondary_paid_amount=Decimal("0.00"),
            payment_status="parcial",
            payment_reference="KEEP-THIS-REFERENCE",
        )
        db.session.add(detail)
        db.session.commit()

        backup, _plan = BackupService.create_manual_backup(company.id, user_id=None)
        db.session.commit()
        payload = BackupService._load_payload(backup)
        payload.pop("purchase_payment_details", None)
        payload["schema_version"] = 2
        Path(backup.path).write_bytes(BackupService._encode_backup_payload(payload))

        with pytest.raises(ValueError, match="respaldo antiguo"):
            BackupService.restore_backup(
                backup,
                expected_company_id=company.id,
                restored_by_user_id=None,
                sections=list(BackupService.FULL_RESTORE_SECTIONS),
            )

        db.session.rollback()
        assert PurchaseOrder.query.filter_by(id=purchase.id, company_id=company.id).count() == 1
        restored_detail = PurchasePaymentDetail.query.filter_by(
            purchase_order_id=purchase.id, company_id=company.id
        ).one()
        assert restored_detail.paid_amount == Decimal("25.00")
        assert restored_detail.payment_reference == "KEEP-THIS-REFERENCE"
