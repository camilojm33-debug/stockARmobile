"""Helpers for SQLite-backed tests."""

from __future__ import annotations

from sqlalchemy import text


def clear_test_data(db):
    db.create_all()
    bind = db.session.get_bind()
    is_sqlite = bool(bind is not None and bind.dialect.name == "sqlite")

    if is_sqlite:
        db.session.execute(text("PRAGMA foreign_keys=OFF"))

    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())

    if is_sqlite:
        db.session.execute(text("PRAGMA foreign_keys=ON"))

    db.session.commit()
