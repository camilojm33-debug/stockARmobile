from types import SimpleNamespace

import app as stock_app

from purchases import _apply_product_purchase_totals


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, locked_rows):
        self.locked_rows = locked_rows
        self.lock_called = False

    def query(self, _model):
        return _FakeQuery(self.locked_rows)


def test_purchase_totals_lock_product_rows_before_mutation(monkeypatch):
    stale = SimpleNamespace(id=1, stock=10.0, cost_price=5.0, price=10.0, margin=0.0, profit_percent=0.0)
    locked = SimpleNamespace(id=1, stock=20.0, cost_price=6.0, price=10.0, margin=0.0, profit_percent=0.0)
    session = _FakeSession([locked])
    monkeypatch.setattr(stock_app, "db", SimpleNamespace(session=session))

    _apply_product_purchase_totals({
        1: [stale, 2.0, 20.0],
    })

    # The calculation must use the freshly locked database row, not the stale
    # object loaded before the transaction acquired the row lock.
    assert locked.stock == 22.0
    assert locked.cost_price == (20.0 * 6.0 + 20.0) / 22.0
    assert stale.stock == 10.0
