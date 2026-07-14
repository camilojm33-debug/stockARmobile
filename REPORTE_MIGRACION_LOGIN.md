# REPORTE_MIGRACION_LOGIN

Fecha: 2026-07-14

## Resultado ejecutivo

El error de Render (UndefinedColumn en users.must_change_password) no es un problema de modelo en codigo: el modelo User si define la columna.

La causa tecnica observada es de ejecucion y estado de esquema:

1. La cadena Alembic de este repositorio es incremental sobre un esquema historico existente.
2. flask db upgrade sobre base totalmente vacia falla antes de llegar a revisiones de users.
3. Si Render no ejecuta las migraciones correctas sobre una base con esquema base existente, o si el servicio quedo con una configuracion de start command distinta a la version actual del repositorio, la columna no se agrega.

## Verificacion solicitada por puntos

### 1) render.yaml

Start command actual en repo:

- python -c "from app import app, db; ctx=app.app_context(); ctx.push(); db.create_all(); ctx.pop()" && python -m flask db upgrade && gunicorn wsgi:application

### 2) flask db upgrade antes de Gunicorn

Si, en el repositorio actual se ejecuta antes de Gunicorn por el orden del startCommand.

### 3) migrations/env.py

Revisado. Usa Flask-Migrate correctamente:

- obtiene engine desde current_app.extensions['migrate']
- configura sqlalchemy.url
- ejecuta context.run_migrations en online/offline

No se detecto problema estructural en env.py.

### 4) alembic.ini

Revisado. Configuracion base correcta para logging Alembic/Flask-Migrate. Sin conflicto detectado.

### 5) migrations/script.py.mako

Revisado. Plantilla estandar de revision Alembic. Sin conflicto detectado.

### 6) migrations/versions/

Revisado el directorio completo.

Revisiones relevantes para la columna:

- 20260713_04_support_tickets_and_password_flag
- 20260714_04_ensure_users_must_change_password
- 20260714_05_enforce_users_must_change_password

### 7) Conexion revision/down_revision/heads/history

Validado:

- No hay multiple heads.
- Head unico actual: 20260714_05_enforce_users_must_change_password.
- History lineal sin ramas paralelas.
- Sin conflictos de revision detectados.

### 8) Prueba desde base vacia con flask db upgrade

Resultado real reproducido:

- flask db upgrade sobre DB totalmente vacia falla en revision 20260712_02 con "no such table: companies".
- Esto confirma que estas migraciones no son una baseline completa para crear todo desde cero.

### 9) Correccion aplicada si no aparece la columna

Se aplicaron correcciones estructurales:

1. Migracion 20260713_04 usa default booleano portable:
- must_change_password BOOLEAN NOT NULL DEFAULT FALSE

2. Se agrego migracion de aseguramiento:
- 20260714_04_ensure_users_must_change_password

3. Se agrego migracion de enforcement posterior:
- 20260714_05_enforce_users_must_change_password

Estas revisiones garantizan que, si existe tabla users y falta la columna, la columna se crea durante upgrade.

### 10) Si la columna aparece, por que Render no la ejecuta

Con los datos disponibles, la explicacion mas probable es una de estas:

1. El servicio Render activo no esta arrancando con el startCommand actual del repo (override en dashboard o deploy anterior).
2. No se completo un deploy nuevo con estas revisiones.
3. La DB productiva tiene estado de revision alembic_version inconsistente con su esquema real (schema drift por stamp/manual).
4. Hubo fallo de migracion en arranque y el servicio quedo ejecutando una version previa.

## Comprobaciones ejecutadas

1. Alembic:
- flask db heads
- flask db history
- flask db current

2. Simulacion de drift productivo:
- Base con esquema, columna users.must_change_password removida manualmente.
- flask db upgrade ejecutado.
- Resultado: columna restaurada correctamente.

3. Login/regresion auth:
- pytest subset auth/roles: 3 passed.

## Estado de los requisitos pedidos

- Si la migracion fallo: si, falla desde DB completamente vacia por falta de baseline completa.
- Si Alembic no la reconoce: no, Alembic reconoce y ejecuta la cadena.
- Multiple heads: no.
- Conflictos de revision: no.
- Si flask db upgrade realmente se ejecuto: si, comprobado localmente.

## SQL obligatorio de verificacion en PostgreSQL Render

Consulta requerida:

```sql
SELECT column_name
FROM information_schema.columns
WHERE table_name='users'
AND column_name='must_change_password';
```

Estado en esta sesion:

- No fue posible ejecutar esta consulta contra Render porque DATABASE_URL de produccion no esta disponible en esta terminal local.
- Evidencia: DATABASE_URL_SET=False.

## Solucion definitiva

1. Mantener startCommand de render.yaml con:
- bootstrap de esquema base (db.create_all)
- luego flask db upgrade
- luego gunicorn

2. Desplegar commit que incluye revisiones 20260714_04 y 20260714_05.

3. Verificar en Render Shell/PostgreSQL:
- alembic_version en head 20260714_05_enforce_users_must_change_password
- query information_schema de users.must_change_password devolviendo 1 fila.

4. Si alembic_version ya estuviera en head y aun falta la columna, ejecutar manualmente en Render SQL una sola vez:

```sql
ALTER TABLE users
ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
```

Luego volver a correr despliegue normal con flask db upgrade.
