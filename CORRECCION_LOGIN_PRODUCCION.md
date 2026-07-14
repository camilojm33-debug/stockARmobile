# CORRECCION_LOGIN_PRODUCCION

Fecha: 2026-07-14

## Causa raiz

El error repetido en produccion (`psycopg2.errors.UndefinedColumn: column users.must_change_password does not exist`) se explica por desalineacion de esquema entre el modelo `User` y la base PostgreSQL activa.

El login y los `before_request` usan `current_user.must_change_password`; cuando la columna no existe en `users`, cualquier carga de usuario (`load_user`) o evaluacion de autenticacion dispara error SQL y termina en 500.

## Auditoria completa solicitada

### app.py

- `User` define `must_change_password = db.Column(db.Boolean, default=False, nullable=False)`.
- `load_user` usa `db.session.get(User, int(user_id))`.
- `before_request` en `enforce_password_change_if_required` evalua `current_user.must_change_password`.

Resultado: el codigo de autenticacion depende estructuralmente de la columna.

### auth.py

- En login se evalua `user.must_change_password` para redirigir a cambio obligatorio.
- En `force_password_change` se setea `current_user.must_change_password = False`.

Resultado: autenticacion funcional requiere columna presente.

### models.py

- No existe archivo `models.py` en este workspace.
- Los modelos estan centralizados en `app.py`.

### Migraciones Alembic

- Existe migracion que agrega la columna:
  - `20260713_04_support_tickets_and_password_flag`
- Se corrigio compatibilidad de default para PostgreSQL y SQLite:
  - `server_default=sa.false()`

Adicionalmente, para blindar bases inconsistentes:
- Se creo migracion de aseguramiento:
  - `20260714_04_ensure_users_must_change_password`
  - agrega `users.must_change_password BOOLEAN NOT NULL DEFAULT FALSE` si falta.

### Flask-Migrate

Verificado operativo:
- `flask db heads` responde correctamente.
- `flask db history` responde correctamente.
- `flask db current` llega a head.

Head final:
- `20260714_04_ensure_users_must_change_password`

### render.yaml

Verificado y ajustado para ejecutar migraciones antes de Gunicorn:
- `startCommand: python -m flask db upgrade && gunicorn wsgi:application`

## Comparacion completa de columnas del modelo User

Columnas en modelo `User`:
- id
- username
- email
- password_hash
- first_name
- last_name
- avatar_url
- auth_provider
- google_sub
- role
- active
- must_change_password
- company_id
- created_at
- updated_at

Matriz Modelo -> Migraciones -> PostgreSQL:

| Columna User | En modelo | En migraciones Alembic | En PostgreSQL Render |
|---|---|---|---|
| id | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| username | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| email | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| password_hash | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| first_name | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| last_name | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| avatar_url | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| auth_provider | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| google_sub | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| role | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| active | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| must_change_password | Si | Si (`20260713_04` + `20260714_04`) | No verificable desde esta sesion local |
| company_id | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| created_at | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |
| updated_at | Si | Base historica (pre-Alembic de este repo) | No verificable desde esta sesion local |

Nota de alcance: en esta terminal local no existe `DATABASE_URL` de Render, por lo que la verificacion directa de PostgreSQL no es ejecutable desde aqui.

## Archivos modificados

- `migrations/versions/20260713_04_support_tickets_and_password_flag.py`
  - default de `must_change_password` corregido a `sa.false()`.
- `migrations/versions/20260714_04_ensure_users_must_change_password.py`
  - nueva migracion de aseguramiento de columna.
- `render.yaml`
  - despliegue con `flask db upgrade` previo a Gunicorn.

## Migraciones creadas

- `20260714_04_ensure_users_must_change_password`

## Pruebas ejecutadas

1. Validacion Alembic/Flask-Migrate
- `flask db heads`
- `flask db history`
- `flask db current`

2. Simulacion de drift real del error
- Se genero base, se elimino manualmente `users.must_change_password`, se marco revision previa y se ejecuto `flask db upgrade`.
- Resultado: la columna se recreo correctamente.

3. Smoke tests de login y roles
- `pytest -q tests/test_smoke.py` -> 31 passed.
- Re-ejecucion focal auth/roles tras cambios -> 4 passed.

## Confirmacion funcional de login

Confirmado por pruebas automatizadas:
- Login usuario: OK
- Login administrador: OK
- Login SuperAdmin: OK
- `current_user`: OK
- `load_user`: OK
- `before_request`: OK

Confirmado en entorno de prueba local:
- Sin `UndefinedColumn`
- Sin `ProgrammingError`
- Sin `Error 500` en rutas auditadas de login/autenticacion

## Accion de despliegue requerida

Desplegar en Render con los cambios y verificar en logs de arranque la ejecucion de:
- `python -m flask db upgrade`

Consulta SQL recomendada post-deploy para verificar produccion:

```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'users'
ORDER BY ordinal_position;
```

Y revision aplicada:

```sql
SELECT version_num FROM alembic_version;
```
