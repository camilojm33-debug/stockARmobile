from datetime import datetime
from decimal import Decimal

from app import Company, Product, Subscription, User, db
import pytest
from promotions import Promotion, _validate_payload
from services.promotion_service import PromotionEngine


@pytest.fixture
def promotion_database(app):
    company = Company(name="Empresa promociones", active=True)
    db.session.add(company)
    db.session.flush()
    user = User(
        username="promo_admin",
        email="promo-admin@test.local",
        password_hash="test-password-hash",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    return {"company": company, "user": user}


def make_product(company_id=1, price="1000", discount="0", category="Bebidas"):
    return Product(
        company_id=company_id,
        barcode="PROMO-" + price + "-" + discount + "-" + category,
        name="Producto promo",
        price=Decimal(price),
        discount=Decimal(discount),
        stock=20,
        active=True,
        category=category,
        unit_measure="u",
        sale_type="unidad",
    )


def make_promotion(company_id=1, created_by_user_id=1, **kwargs):
    values = dict(
        company_id=company_id,
        name="Promo test",
        type="bogo",
        buy_quantity=Decimal("2"),
        pay_quantity=Decimal("1"),
        active=True,
        status="ACTIVA",
        priority=100,
        created_by_user_id=created_by_user_id,
    )
    values.update(kwargs)
    return Promotion(**values)


def test_promotion_engine_2x1(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id)
    db.session.add(product)
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id))
    db.session.commit()

    result = PromotionEngine.evaluate(company_id=company_id, product=product, quantity=Decimal("4"))

    assert result.paid_quantity == Decimal("2")
    assert result.free_quantity == Decimal("2")
    assert result.promotion_discount == Decimal("2000.00")
    assert result.final_amount == Decimal("2000.00")


def test_promotion_engine_3x2_remainder(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id, price="500")
    db.session.add(product)
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id, buy_quantity=Decimal("3"), pay_quantity=Decimal("2")))
    db.session.commit()

    result = PromotionEngine.evaluate(company_id=company_id, product=product, quantity=Decimal("7"))

    assert result.free_quantity == Decimal("2")
    assert result.paid_quantity == Decimal("5")
    assert result.final_amount == Decimal("2500.00")


def test_promotion_engine_4x2(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id)
    db.session.add(product)
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id, buy_quantity=Decimal("4"), pay_quantity=Decimal("2")))
    db.session.commit()

    result = PromotionEngine.evaluate(company_id=company_id, product=product, quantity=Decimal("8"))

    assert result.free_quantity == Decimal("4")
    assert result.paid_quantity == Decimal("4")
    assert result.final_amount == Decimal("4000.00")


def test_promotion_engine_quantity_percentage(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id)
    db.session.add(product)
    db.session.add(make_promotion(
        name="3 unidades 15%",
        type="percent_quantity",
        buy_quantity=None,
        pay_quantity=None,
        min_quantity=Decimal("3"),
        discount_percent=Decimal("15"),
        company_id=company_id,
        created_by_user_id=user_id,
    ))
    db.session.commit()

    result = PromotionEngine.evaluate(company_id=company_id, product=product, quantity=Decimal("3"))

    assert result.promotion_discount == Decimal("450.00")
    assert result.final_amount == Decimal("2550.00")


def test_promotion_engine_expired_is_ignored(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id)
    db.session.add(product)
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id, ends_at=datetime(2026, 1, 1)))
    db.session.commit()

    result = PromotionEngine.evaluate(
        company_id=company_id,
        product=product,
        quantity=Decimal("2"),
        now=datetime(2026, 9, 22),
    )

    assert result.promotion_id is None
    assert result.final_amount == Decimal("2000.00")


def test_promotion_engine_preserves_legacy_product_discount(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id, discount="100")
    db.session.add(product)
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id))
    db.session.commit()

    result = PromotionEngine.evaluate(company_id=company_id, product=product, quantity=Decimal("2"))

    assert result.legacy_discount == Decimal("200.00")
    assert result.promotion_discount == Decimal("900.00")
    assert result.final_amount == Decimal("900.00")



def test_promotion_engine_product_specific_wins_category_on_tie(promotion_database):
    company_id = promotion_database["company"].id
    user_id = promotion_database["user"].id
    product = make_product(company_id=company_id, category="Bebidas")
    db.session.add(product)
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id, name="Promo categoría", buy_quantity=Decimal("2"), pay_quantity=Decimal("1"), priority=100, category="Bebidas", product_id=None))
    db.session.add(make_promotion(company_id=company_id, created_by_user_id=user_id, name="Promo producto", buy_quantity=Decimal("3"), pay_quantity=Decimal("2"), priority=100, product_id=product.id, category=None))
    db.session.commit()
    result = PromotionEngine.evaluate(company_id=company_id, product=product, quantity=Decimal("3"))
    assert result.promotion_name == "Promo producto"
    assert result.free_quantity == Decimal("1")


def test_promotion_standard_quote_flow_is_connected():
    from pathlib import Path
    source = Path("quotes.py").read_text(encoding="utf-8")
    assert "from services.promotion_service import PromotionEngine" in source
    assert "PromotionEngine.apply_to_lines" in source
    assert "promotion_reason" in source


def test_promotion_rule_form_is_contextual_and_preserves_values():
    from pathlib import Path
    source = Path("templates/promociones/index.html").read_text(encoding="utf-8")
    assert 'id="ruleBogo"' in source
    assert 'id="rulePercent"' in source
    assert 'id="ruleFixed"' in source
    assert "form_data.get('buy_quantity'" in source
    assert 'input.disabled=!enabled' in source

def test_promotion_access_is_business_only(promotion_database):
    from app import Company, Plan, Subscription
    from services.ai_agent.usage_service import can_use_commercial_feature
    from services.plan_service import PlanService

    PlanService.ensure_defaults(db.session)
    company = promotion_database["company"]
    business = Plan.query.filter_by(code="business").first()
    entrepreneur = Plan.query.filter_by(code="entrepreneur").first()
    subscription = Subscription(company_id=company.id, plan_id=business.id, status="active")
    db.session.add(subscription)
    db.session.commit()

    assert can_use_commercial_feature(company, "promotions").allowed is True

    subscription.plan_id = entrepreneur.id
    db.session.commit()
    assert can_use_commercial_feature(company, "promotions").allowed is False


def test_promotions_menu_and_route_are_registered():
    from pathlib import Path
    from app import app

    source = Path("templates/base_master.html").read_text(encoding="utf-8")
    assert "Promociones" in source
    assert "commercial_promotions_allowed" in source
    assert "can_use_commercial_feature(company, \"promotions\").allowed" in Path("app.py").read_text(encoding="utf-8")
    assert "url_for('promotions.index')" in source
    assert any(rule.endpoint == "promotions.index" for rule in app.url_map.iter_rules())


def test_promotion_rule_validation_accepts_2x1():
    payload = _validate_payload({
        "type": "bogo",
        "product_id": "10",
        "buy_quantity": "2",
        "pay_quantity": "1",
        "name": "2x1",
        "active": "1",
        "priority": "100",
    })
    assert payload["buy_quantity"] == Decimal("2")
    assert payload["pay_quantity"] == Decimal("1")


def test_promotion_rule_validation_explains_missing_bogo_rule():
    with pytest.raises(ValueError, match="Completá la regla"):
        _validate_payload({
            "type": "bogo",
            "product_id": "10",
            "name": "2x1",
            "active": "1",
            "priority": "100",
        })


def test_promotion_rule_validation_accepts_quantity_percentage():
    payload = _validate_payload({
        "type": "percent_quantity",
        "category": "Bebidas",
        "min_quantity": "3",
        "discount_percent": "15",
        "name": "3 unidades 15%",
        "active": "1",
        "priority": "100",
    })
    assert payload["min_quantity"] == Decimal("3")
    assert payload["discount_percent"] == Decimal("15")
