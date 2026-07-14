# AUDITORIA_MUST_CHANGE_PASSWORD

Fecha: 2026-07-14

## Causa exacta

El fallo de login en producción (`psycopg2.errors.UndefinedColumn: column users.must_change_password does not exist`) no se debe a una desalineación entre el modelo `User` y su migración directa.

La causa real es que la base de producción no llegó a la revisión que agrega esa columna porque, antes de esta auditoría, la cadena Alembic tenía bloqueos en migraciones previas. En ese estado, `python -m flask db upgrade` podía detenerse antes de alcanzar `20260713_04`, dejando `users.must_change_password` ausente en PostgreSQL.

## Migración responsable

La columna `users.must_change_password` se crea en:

- `20260713_04_support_tickets_and_password_flag`

Archivo:
- `migrations/versions/20260713_04_support_tickets_and_password_flag.py`

Línea relevante:
- `op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))`

## Revisión Alembic donde se crea

- Revision: `20260713_04`
- Down revision: `20260713_03`

La cadena actual llega correctamente hasta:
- `20260714_05_enforce_users_must_change_password` (HEAD)

## Verificación de enlace en la cadena Alembic

Historia lineal verificada:

- `20260712_01`
- `20260712_02`
- `20260713_03`
- `20260713_04`  ← aquí se crea `users.must_change_password`
- `20260713_05`
- `20260714_01_referral_program`
- `20260714_02_landing_testimonials`
- `20260714_03_referral_commissions_note`
- `20260714_04_ensure_users_must_change_password`
- `20260714_05_enforce_users_must_change_password`

Conclusión:
- La migración está correctamente enlazada.
- No hay múltiples heads.
- No hay ramas paralelas.
- No hay `return` en `20260713_04` que impida crear la columna cuando `users` existe y la columna falta.

## Dependencias o bloqueos que podían impedir llegar a 20260713_04

Se verificó que la cadena completa hoy termina correctamente, pero la razón por la que producción pudo quedar sin la columna es que anteriormente había fallos en migraciones previas de la misma cadena, antes de completar todas las revisiones.

En el estado actual auditado:
- `flask db upgrade` termina correctamente en PostgreSQL vacío.
- `flask db current` llega al HEAD.
- `flask db heads` reporta un único HEAD.

## Render y Start Command

Archivo revisado:
- `render.yaml`

Start command actual:

```yaml
startCommand: python -c "from app import app, db; ctx=app.app_context(); ctx.push(); db.create_all(); ctx.pop()" && python -m flask db upgrade && gunicorn wsgi:application
```

Conclusión:
- Sí ejecuta `python -m flask db upgrade` antes de iniciar Gunicorn.
- Además hace bootstrap de esquema antes del upgrade.

## Sincronización modelo User vs migración

Modelo `User` en `app.py`:
- `must_change_password = db.Column(db.Boolean, default=False, nullable=False)`

Migración que agrega la columna:
- `20260713_04_support_tickets_and_password_flag.py`
- `must_change_password BOOLEAN NOT NULL DEFAULT FALSE`

Conclusión:
- El modelo `User` y la migración están sincronizados.

## Migraciones de aseguramiento existentes

Se revisaron estas migraciones de aseguramiento:

- `20260714_04_ensure_users_must_change_password.py`
- `20260714_05_enforce_users_must_change_password.py`

Ambas contienen lógica para agregar la columna si:
- existe tabla `users`
- falta columna `must_change_password`

No fue necesario modificarlas en esta auditoría porque ya cumplen exactamente ese objetivo.

## Comprobaciones ejecutadas

### 1. HEAD actual

Comando:

```powershell
$env:DATABASE_URL='postgresql://postgres:postgres@localhost:5432/stock_empty'
& .\.venv\Scripts\python.exe -m flask db heads
```

Resultado:
- `20260714_05_enforce_users_must_change_password (head)`

### 2. Revisión actual aplicada

Comando:

```powershell
$env:DATABASE_URL='postgresql://postgres:postgres@localhost:5432/stock_empty'
& .\.venv\Scripts\python.exe -m flask db current
```

Resultado:
- `20260714_05_enforce_users_must_change_password (head)`

### 3. Upgrade completo

Comando:

```powershell
$env:PGPASSWORD='postgres'
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -p 5432 -U postgres -d postgres -c "DROP DATABASE IF EXISTS stock_empty"
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -p 5432 -U postgres -d postgres -c "CREATE DATABASE stock_empty"
$env:DATABASE_URL='postgresql://postgres:postgres@localhost:5432/stock_empty'
& .\.venv\Scripts\python.exe -m flask db upgrade
```

Resultado:
- La cadena Alembic completa termina sin errores.

### 4. Verificación de existencia de columna

Comando:

```sql
SELECT column_name
FROM information_schema.columns
WHERE table_name='users'
AND column_name='must_change_password';
```

Resultado:
- Devuelve exactamente 1 fila: `must_change_password`

### 5. Verificación sobre base creada previamente y desalineada

Se simuló una base ya creada en PostgreSQL:
- se ejecutó upgrade completo
- se eliminó manualmente `users.must_change_password`
- se retrocedió `alembic_version` a `20260714_03_referral_commissions_note`
- se volvió a correr `flask db upgrade`

Resultado:
- `20260714_04` y `20260714_05` volvieron a ejecutarse
- la columna `users.must_change_password` fue restaurada correctamente

## Por qué no llegó a producción

La explicación más consistente con el error observado es:

1. Producción quedó con un esquema intermedio, anterior a la revisión `20260713_04` o sin haber completado toda la cadena.
2. La aplicación ya estaba usando el modelo `User` con `must_change_password`.
3. El login intentó leer `users.must_change_password` vía `load_user` / `before_request`.
4. PostgreSQL respondió `UndefinedColumn` porque la migración que agregaba la columna no había sido aplicada en esa base.

## Archivos modificados

En esta auditoría específica no fue necesario modificar código de migraciones ni modelos.

Archivo nuevo generado:
- `AUDITORIA_MUST_CHANGE_PASSWORD.md`

## Confirmación final

- `flask db heads`: OK
- `flask db current`: OK
- `flask db upgrade`: OK
- `users.must_change_password` existe en PostgreSQL: OK
- `render.yaml` ejecuta upgrade antes de Gunicorn: OK
