# AUDITORIA_CREATE_ALL_PRODUCCION

Fecha: 2026-07-14

## Objetivo

Verificar si `db.create_all()` estaba interfiriendo con Alembic en producción y si una instalación limpia puede realizarse exclusivamente con:

- `python -m flask db upgrade`

## Revisión realizada

### render.yaml

Estado anterior del despliegue:

```yaml
startCommand: python -c "from app import app, db; ctx=app.app_context(); ctx.push(); db.create_all(); ctx.pop()" && python -m flask db upgrade && gunicorn wsgi:application
```

Estado corregido:

```yaml
startCommand: python -m flask db upgrade && gunicorn wsgi:application
```

Conclusión:
- Sí existía uso explícito de `db.create_all()` en el despliegue de producción.
- Ese uso fue eliminado del `startCommand` para que el esquema quede administrado exclusivamente por Alembic.

### app.py

Se revisó la inicialización de la aplicación.

Hallazgos:
- No existe `create_app()` en este proyecto.
- La app se inicializa de forma global en `app.py`.
- No hay `db.create_all()` automático en el arranque de la app.
- Existe `ensure_database_schema()`, pero está deshabilitada explícitamente:
  - `"Compatibilidad retroactiva: deshabilitado para forzar migraciones Alembic."`

Conclusión:
- Fuera de `render.yaml`, no se detectó un `db.create_all()` automático activo en producción.

### Inicialización automática

No se detectaron otros puntos de inicialización automática de esquema en producción dentro de `app.py`.

## ¿db.create_all() podía interferir con Alembic?

Sí, potencialmente.

Razón:
- `db.create_all()` crea tablas directamente desde el modelo SQLAlchemy, sin registrar revisiones en `alembic_version`.
- Si se ejecuta antes de `flask db upgrade`, puede dejar una base en estado parcialmente creado por metadatos, pero no verdaderamente migrado por Alembic.
- Eso complica el diagnóstico porque puede existir parte del esquema sin que la cadena Alembic haya recorrido formalmente todas las revisiones.

Conclusión operativa:
- Si Alembic administra el esquema, `db.create_all()` no debe formar parte del despliegue.

## Confirmación de instalación limpia sin create_all

Se validó una instalación limpia sobre PostgreSQL vacío ejecutando solo:

```powershell
$env:DATABASE_URL='postgresql://postgres:postgres@localhost:5432/stock_render_no_create_all'
& .\.venv\Scripts\python.exe -m flask db upgrade
```

Resultado:
- La cadena Alembic completa terminó correctamente.
- La columna `users.must_change_password` quedó creada.

Consulta ejecutada:

```sql
SELECT column_name
FROM information_schema.columns
WHERE table_name='users'
AND column_name='must_change_password';
```

Resultado:
- Devuelve exactamente 1 fila: `must_change_password`.

## Conclusión final

- `db.create_all()` sí estaba presente en el despliegue de producción vía `render.yaml`.
- No era necesario para una instalación limpia una vez corregida la cadena de migraciones.
- Podía contribuir a una base parcialmente creada fuera del control explícito de Alembic.
- El despliegue quedó corregido para depender exclusivamente de:
  - `python -m flask db upgrade`
  - luego `gunicorn`

## Archivos modificados

- `render.yaml`
- `AUDITORIA_CREATE_ALL_PRODUCCION.md`
