# INFORME_AUDITORIA_BOOLEAN_ALEMBIC

Fecha: 2026-07-14

## Alcance

Se auditaron todas las migraciones Alembic del directorio `migrations/versions/` buscando columnas `BOOLEAN` que usaran defaults incompatibles con PostgreSQL:

- `server_default=sa.text("1")`
- `server_default=sa.text("0")`
- `DEFAULT 1`
- `DEFAULT 0`

## Migraciones revisadas

- `20260712_01_products_barcode_company_unique.py`
- `20260712_02_hardening_numeric_money_columns.py`
- `20260713_03_company_profile_and_security_pin.py`
- `20260713_04_support_tickets_and_password_flag.py`
- `20260713_05_password_recovery_requests.py`
- `20260714_01_referral_program_tables.py`
- `20260714_02_landing_testimonials.py`
- `20260714_03_referral_commissions_note.py`
- `20260714_04_ensure_users_must_change_password.py`
- `20260714_05_enforce_users_must_change_password.py`

## Columnas BOOLEAN auditadas

1. `20260713_04_support_tickets_and_password_flag.py`
- Línea 41
- Columna: `users.must_change_password`
- Estado final: `server_default=sa.false()`

2. `20260714_01_referral_program_tables.py`
- Línea 58
- Columna: `referral_sellers.active`
- Estado final: `server_default=sa.true()`

3. `20260714_02_landing_testimonials.py`
- Línea 40
- Columna: `landing_testimonials.active`
- Estado final: `server_default=sa.true()`

4. `20260714_04_ensure_users_must_change_password.py`
- Línea 33
- Columna: `users.must_change_password`
- Estado final: `server_default=sa.false()`

5. `20260714_05_enforce_users_must_change_password.py`
- Línea 35
- Columna: `users.must_change_password`
- Estado final: `server_default=sa.false()`

## Lineas modificadas en esta auditoria

No fue necesario realizar modificaciones adicionales en esta pasada.

Resultado de la auditoria actual:
- No quedan migraciones con columnas `BOOLEAN` usando `sa.text("1")`, `sa.text("0")`, `DEFAULT 1` o `DEFAULT 0` dentro de `migrations/versions/`.

## Observacion de alcance

Se detecto un `server_default="0"` en `20260713_03_company_profile_and_security_pin.py`, pero corresponde a una columna `INTEGER` (`business_pin_failed_attempts`), no a una columna `BOOLEAN`, por lo que no se modifico.

## Validacion ejecutada

Se ejecuto sobre PostgreSQL vacio local:

```powershell
$env:PGPASSWORD='postgres'
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -p 5432 -U postgres -d postgres -c "DROP DATABASE IF EXISTS stock_empty"
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -p 5432 -U postgres -d postgres -c "CREATE DATABASE stock_empty"
$env:DATABASE_URL='postgresql://postgres:postgres@localhost:5432/stock_empty'
& .\.venv\Scripts\python.exe -m flask db upgrade
```

## Resultado de validacion

- `flask db upgrade` completa toda la cadena Alembic sin errores en PostgreSQL vacio.
- No se reprodujeron errores de `DatatypeMismatch` por defaults booleanos enteros.
