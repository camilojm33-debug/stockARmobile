"""Tenant-scoped locks and durable results for public Vendor operations."""

from __future__ import annotations

import hashlib
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import PublicVendorOperation

_local_lock_guard = threading.Lock()
_local_locks: dict[tuple, tuple[threading.Lock, int]] = {}


def _lock_value(*, scope: str, company_id: int, conversation_id: int | str, key: str) -> int:
    value = f"stockarmobile:public-vendor:{scope}:{int(company_id)}:{conversation_id}:{key}"
    raw = int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big")
    return raw - (1 << 64) if raw >= (1 << 63) else raw


@contextmanager
def public_operation_lock(*, scope: str, company_id: int, conversation_id: int | str, key: str):
    """Serialize a visitor or request across workers; use a process lock on SQLite."""
    lock_value = _lock_value(
        scope=scope,
        company_id=company_id,
        conversation_id=conversation_id,
        key=key,
    )
    bind = db.session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        connection = db.engine.connect()
        acquired = False
        try:
            connection.execute(db.text("SELECT pg_advisory_lock(:key)"), {"key": lock_value})
            connection.commit()
            acquired = True
            yield
        finally:
            if acquired:
                try:
                    connection.execute(db.text("SELECT pg_advisory_unlock(:key)"), {"key": lock_value})
                    connection.commit()
                finally:
                    connection.close()
            else:
                connection.close()
        return

    local_key = (scope, int(company_id), str(conversation_id), str(key))
    with _local_lock_guard:
        lock, references = _local_locks.get(local_key, (threading.Lock(), 0))
        _local_locks[local_key] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _local_lock_guard:
            current = _local_locks.get(local_key)
            if current is not None and current[1] <= 1:
                _local_locks.pop(local_key, None)
            elif current is not None:
                _local_locks[local_key] = (current[0], current[1] - 1)


class PublicOperationConflict(ValueError):
    pass


def get_or_create_operation(
    *,
    company_id: int,
    conversation_id: int,
    idempotency_key: str,
    operation_type: str,
    trace_id: str | None = None,
) -> PublicVendorOperation:
    operation = PublicVendorOperation.query.filter_by(
        company_id=int(company_id),
        conversation_id=int(conversation_id),
        idempotency_key=str(idempotency_key),
    ).with_for_update().first()
    if operation is not None:
        if operation.operation_type != operation_type:
            raise PublicOperationConflict("La clave de idempotencia ya se usó para otra operación.")
        operation.status = "processing"
        operation.updated_at = utcnow_naive()
        return operation

    operation = PublicVendorOperation(
        company_id=int(company_id),
        conversation_id=int(conversation_id),
        idempotency_key=str(idempotency_key),
        operation_type=operation_type,
        status="processing",
        trace_id=str(trace_id or uuid.uuid4().hex),
    )
    db.session.add(operation)
    db.session.flush()
    return operation


def complete_operation(operation: PublicVendorOperation, result: dict, *, quote_id: int | None = None) -> None:
    operation.status = "completed"
    operation.result_json = result
    operation.quote_id = quote_id
    operation.updated_at = datetime.utcnow()


def cached_result(operation: PublicVendorOperation | None, *, operation_type: str) -> dict | None:
    if operation is None or operation.operation_type != operation_type or operation.status != "completed":
        return None
    return dict(operation.result_json) if isinstance(operation.result_json, dict) else None