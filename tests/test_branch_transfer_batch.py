import json
from types import SimpleNamespace

from app import (
    Branch,
    BranchStockMovement,
    BranchStockTransfer,
    Company,
    Product,
    User,
    db,
)
from stockarmobile.branch_inventory import available_stock, set_branch_stock


def _company(name):
    company = Company(name=name, active=True)
    db.session.add(company)
    db.session.flush()
    return company


def _branch(company, name, code):
    branch = Branch(company_id=company.id, name=name, code=code, active=True)
    db.session.add(branch)
    db.session.flush()
    return branch


def _admin(company, suffix="one"):
    user = User(
        username=f"transfer-admin-{suffix}",
        email=f"transfer-admin-{suffix}@example.test",
        password_hash="test-hash",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _allow_feature(monkeypatch):
    import services.ai_agent.usage_service as usage_service

    monkeypatch.setattr(
        usage_service,
        "can_use_commercial_feature",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason=None),
    )


def _product(company, barcode, name, category, stock):
    product = Product(
        company_id=company.id,
        barcode=barcode,
        name=name,
        category=category,
        stock=stock,
        min_stock=0,
        active=True,
    )
    db.session.add(product)
    db.session.flush()
    return product


def test_batch_branch_transfer_moves_multiple_products_in_one_operation(app):
    with app.app_context():
        company = _company("Empresa transferencia múltiple")
        source = _branch(company, "Casa central", "CENTRAL")
        target = _branch(company, "25 de Mayo", "25M")
        first = _product(company, "TR-BATCH-001", "Leche", "Lácteos", 12)
        second = _product(company, "TR-BATCH-002", "Arroz", "Almacén", 8)
        set_branch_stock(first, source.id, 10)
        set_branch_stock(first, target.id, 2)
        set_branch_stock(second, source.id, 6)
        set_branch_stock(second, target.id, 2)

        from stockarmobile.branch_inventory import transfer_branch_stock_batch

        transfers = transfer_branch_stock_batch(
            [(second, "3"), (first, "4.5")],
            source.id,
            target.id,
            note="Reposición mensual",
        )
        db.session.commit()

        assert len(transfers) == 2
        assert available_stock(first, source.id) == 5.5
        assert available_stock(first, target.id) == 6.5
        assert available_stock(second, source.id) == 3
        assert available_stock(second, target.id) == 5
        assert float(first.stock) == 12
        assert float(second.stock) == 8
        assert BranchStockTransfer.query.filter_by(company_id=company.id).count() == 2
        transfer_ids = [row.id for row in transfers]
        movements = BranchStockMovement.query.filter(
            BranchStockMovement.company_id == company.id,
            BranchStockMovement.source_type == "branch_transfer",
            BranchStockMovement.source_id.in_(transfer_ids),
        ).all()
        assert len(movements) == 4
        assert {row.movement_type for row in movements} == {"transfer_out", "transfer_in"}


def test_batch_branch_transfer_can_be_rolled_back_when_one_product_has_insufficient_stock(app):
    with app.app_context():
        company = _company("Empresa transferencia atómica")
        source = _branch(company, "Casa central", "CENTRAL")
        target = _branch(company, "Sucursal", "SUC")
        first = _product(company, "TR-ATOMIC-001", "Producto suficiente", "General", 10)
        second = _product(company, "TR-ATOMIC-002", "Producto insuficiente", "General", 5)
        set_branch_stock(first, source.id, 8)
        set_branch_stock(first, target.id, 2)
        set_branch_stock(second, source.id, 1)
        set_branch_stock(second, target.id, 4)
        # Persist fixture data before testing the transfer transaction rollback.
        # Otherwise rollback would also remove the newly-created company/branches.
        db.session.commit()

        from stockarmobile.branch_inventory import transfer_branch_stock_batch

        try:
            transfer_branch_stock_batch(
                [(first, "3"), (second, "2")],
                source.id,
                target.id,
                note="Debe revertirse toda la tanda",
            )
        except ValueError as exc:
            assert "Stock insuficiente" in str(exc)
            db.session.rollback()
        else:
            raise AssertionError("La transferencia debía fallar por stock insuficiente.")

        assert available_stock(first, source.id) == 8
        assert available_stock(first, target.id) == 2
        assert available_stock(second, source.id) == 1
        assert available_stock(second, target.id) == 4
        assert BranchStockTransfer.query.filter_by(company_id=company.id).count() == 0
        assert BranchStockMovement.query.filter_by(
            company_id=company.id, source_type="branch_transfer"
        ).count() == 0


def test_batch_branch_transfer_rejects_duplicate_products_before_moving_stock(app):
    with app.app_context():
        company = _company("Empresa transferencia duplicados")
        source = _branch(company, "Casa central", "CENTRAL")
        target = _branch(company, "Sucursal", "SUC")
        product = _product(company, "TR-DUP-001", "Producto único", "General", 5)
        set_branch_stock(product, source.id, 5)
        set_branch_stock(product, target.id, 0)

        from stockarmobile.branch_inventory import transfer_branch_stock_batch

        try:
            transfer_branch_stock_batch(
                [(product, "1"), (product, "2")],
                source.id,
                target.id,
            )
        except ValueError as exc:
            assert "No repitas" in str(exc)
        else:
            raise AssertionError("La transferencia debía rechazar productos duplicados.")

        assert available_stock(product, source.id) == 5
        assert available_stock(product, target.id) == 0


def test_branch_transfer_product_search_returns_only_tenant_and_source_branch_stock(app, monkeypatch):
    _allow_feature(monkeypatch)
    with app.app_context():
        company = _company("Empresa catálogo transferencia")
        other_company = _company("Otra empresa catálogo")
        source = _branch(company, "Casa central", "CENTRAL")
        _branch(company, "Destino", "DEST")
        product = _product(company, "TR-SEARCH-001", "Leche entera", "Lácteos", 9)
        no_stock = _product(company, "TR-SEARCH-002", "Queso crema", "Lácteos", 0)
        foreign = _product(other_company, "TR-SEARCH-003", "Leche ajena", "Lácteos", 20)
        set_branch_stock(product, source.id, 7)
        set_branch_stock(no_stock, source.id, 0)
        db.session.commit()
        user = _admin(company, "search")
        db.session.commit()
        client = app.test_client()
        _login(client, user)

        response = client.get(
            f"/sucursales/api/productos-transferencia?source_branch_id={source.id}&q=leche"
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["success"] is True
        assert [row["id"] for row in payload["products"]] == [product.id]
        assert payload["products"][0]["stock"] == 7
        assert payload["products"][0]["category"] == "Lácteos"
        assert "Leche ajena" not in [row["name"] for row in payload["products"]]
        assert foreign.company_id != company.id


def test_branch_transfer_post_processes_all_selected_products_together(app, monkeypatch):
    import branches as branches_module

    monkeypatch.setattr(branches_module, "_require_feature", lambda _company: None)
    monkeypatch.setattr(branches_module, "_require_company_pin", lambda _company: None)
    with app.app_context():
        company = _company("Empresa transferencia ruta")
        source = _branch(company, "Casa central", "CENTRAL")
        target = _branch(company, "25 de Mayo", "25M")
        first = _product(company, "TR-POST-001", "Aceite", "Almacén", 10)
        second = _product(company, "TR-POST-002", "Fideos", "Almacén", 6)
        set_branch_stock(first, source.id, 8)
        set_branch_stock(first, target.id, 2)
        set_branch_stock(second, source.id, 5)
        set_branch_stock(second, target.id, 1)
        user = _admin(company, "post")
        db.session.commit()

        client = app.test_client()
        _login(client, user)
        response = client.post(
            "/sucursales/transferir-stock",
            data={
                "source_branch_id": str(source.id),
                "target_branch_id": str(target.id),
                "note": "Reposición de salón",
                "items_json": json.dumps([
                    {"product_id": first.id, "quantity": "3"},
                    {"product_id": second.id, "quantity": "2.5"},
                ]),
            },
        )

        assert response.status_code == 302
        assert available_stock(first, source.id) == 5
        assert available_stock(first, target.id) == 5
        assert available_stock(second, source.id) == 2.5
        assert available_stock(second, target.id) == 3.5
        assert BranchStockTransfer.query.filter_by(company_id=company.id).count() == 2


def test_branch_transfer_post_rolls_back_every_line_if_one_product_fails(app, monkeypatch):
    import branches as branches_module

    monkeypatch.setattr(branches_module, "_require_feature", lambda _company: None)
    monkeypatch.setattr(branches_module, "_require_company_pin", lambda _company: None)
    with app.app_context():
        company = _company("Empresa transferencia ruta atómica")
        source = _branch(company, "Casa central", "CENTRAL")
        target = _branch(company, "25 de Mayo", "25M")
        first = _product(company, "TR-POST-ATOMIC-001", "Stock suficiente", "General", 8)
        second = _product(company, "TR-POST-ATOMIC-002", "Stock corto", "General", 5)
        set_branch_stock(first, source.id, 6)
        set_branch_stock(first, target.id, 2)
        set_branch_stock(second, source.id, 1)
        set_branch_stock(second, target.id, 4)
        user = _admin(company, "atomic")
        db.session.commit()

        client = app.test_client()
        _login(client, user)
        response = client.post(
            "/sucursales/transferir-stock",
            data={
                "source_branch_id": str(source.id),
                "target_branch_id": str(target.id),
                "items_json": json.dumps([
                    {"product_id": first.id, "quantity": "2"},
                    {"product_id": second.id, "quantity": "2"},
                ]),
            },
        )

        assert response.status_code == 302
        assert available_stock(first, source.id) == 6
        assert available_stock(first, target.id) == 2
        assert available_stock(second, source.id) == 1
        assert available_stock(second, target.id) == 4
        assert BranchStockTransfer.query.filter_by(company_id=company.id).count() == 0
        assert BranchStockMovement.query.filter_by(
            company_id=company.id, source_type="branch_transfer"
        ).count() == 0


def test_branch_transfer_settings_render_multi_product_selector_and_script(app):
    from pathlib import Path

    template = (Path(__file__).resolve().parents[1] / "templates" / "company_billing" / "settings.html").read_text(encoding="utf-8")
    script = (Path(__file__).resolve().parents[1] / "static" / "assets" / "js" / "branch-transfer-manager.js").read_text(encoding="utf-8")

    assert 'id="branch-transfer-manager"' in template
    assert 'name="items_json"' in template
    assert "branch-transfer-manager.js" in template
    assert "Agregar visibles" in template
    assert "data-transfer-quantity" in script
    assert "items_json" in template
    assert "available_only" in script
