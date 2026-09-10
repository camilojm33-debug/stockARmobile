import importlib
from decimal import Decimal


def test_referral_network_module_loads():
    module = importlib.import_module("services.referral_network_service")
    assert module.DEFAULT_PARENT_PERCENT == Decimal("0.3000")
    assert module.MAX_PARENT_PERCENT == Decimal("0.5000")
    assert module._percent(Decimal("0.30")) == Decimal("0.3000")
    assert module._percent(Decimal("0.50")) == Decimal("0.5000")
    assert module._percent(Decimal("0.40")) == Decimal("0.3000")
