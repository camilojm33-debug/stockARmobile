# ERROR_REAL_MIGRACION_20260712_02

Fecha: 2026-07-14

## 1) Migracion exacta donde se detenia

Primera detencion reproducida ejecutando exactamente `flask db upgrade` sobre PostgreSQL vacia:

- `20260712_02_hardening_numeric_money_columns`

## 2) Traceback completo (falla real)

```text
NOTICE:  no existe la base de datos «stock_empty», omitiendo
DROP DATABASE
CREATE DATABASE
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 20260712_01, Make product barcode unique per company.
INFO  [alembic.runtime.migration] Running upgrade 20260712_01 -> 20260712_02, Harden monetary columns using NUMERIC and add company QR payment fields.
Traceback (most recent call last):
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 1969, in _exec_single_context
    self.dialect.do_execute(
    ~~~~~~~~~~~~~~~~~~~~~~~^
        cursor, str_statement, effective_parameters, context
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\default.py", line 952, in do_execute
    cursor.execute(statement, parameters)
    ~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
psycopg2.errors.UndefinedTable: no existe la relación «companies»


The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\flask\__main__.py", line 3, in <module>
    main()
    ~~~~^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\flask\cli.py", line 1131, in main
    cli.main()
    ~~~~~~~~^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\core.py", line 1490, in main
    rv = self.invoke(ctx)
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\core.py", line 1970, in invoke
    return _process_result(sub_ctx.command.invoke(sub_ctx))
                           ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\core.py", line 1970, in invoke
    return _process_result(sub_ctx.command.invoke(sub_ctx))
                           ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\core.py", line 1353, in invoke
    return ctx.invoke(self.callback, **ctx.params)
           ~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\core.py", line 907, in invoke
    return callback(*args, **kwargs)
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\decorators.py", line 34, in new_func
    return f(get_current_context(), *args, **kwargs)
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\flask\cli.py", line 400, in decorator
    return ctx.invoke(f, *args, **kwargs)
           ~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\click\core.py", line 907, in invoke
    return callback(*args, **kwargs)
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\flask_migrate\cli.py", line 157, in upgrade
    _upgrade(directory or g.directory, revision, sql, tag, x_arg or g.x_arg)
    ~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\flask_migrate\__init__.py", line 111, in wrapped
    f(*args, **kwargs)
    ~^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\flask_migrate\__init__.py", line 200, in upgrade
    command.upgrade(config, revision, sql=sql, tag=tag)
    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\command.py", line 490, in upgrade
    script.run_env()
    ~~~~~~~~~~~~~~^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\script\base.py", line 556, in run_env
    util.load_python_file(self.dir, "env.py")
    ~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\util\pyfiles.py", line 116, in load_python_file
    module = load_module_py(module_id, path)
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\util\pyfiles.py", line 136, in load_module_py
    spec.loader.exec_module(module)  # type: ignore
    ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^
  File "<frozen importlib._bootstrap_external>", line 759, in exec_module
  File "<frozen importlib._bootstrap>", line 491, in _call_with_frames_removed
  File "C:\Users\USUARIO\Desktop\stock ultimate\migrations\env.py", line 113, in <module>
    run_migrations_online()
    ~~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\migrations\env.py", line 107, in run_migrations_online
    context.run_migrations()
    ~~~~~~~~~~~~~~~~~~~~~~^^
  File "<string>", line 8, in run_migrations
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\runtime\environment.py", line 969, in run_migrations
    self.get_context().run_migrations(**kw)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\runtime\migration.py", line 626, in run_migrations
    step.migration_fn(**kw)
    ~~~~~~~~~~~~~~~~~^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\migrations\versions\20260712_02_hardening_numeric_money_columns.py", line 127, in upgrade
    _add_company_qr_columns(bind)
    ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\migrations\versions\20260712_02_hardening_numeric_money_columns.py", line 27, in _add_company_qr_columns
    op.add_column("companies", sa.Column("payment_alias", sa.String(length=120), nullable=True))
    ~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<string>", line 8, in add_column
  File "<string>", line 3, in add_column
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\operations\ops.py", line 2258, in add_column
    return operations.invoke(op)
           ~~~~~~~~~~~~~~~~~^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\operations\base.py", line 452, in invoke
    return fn(self, operation)
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\operations\toimpl.py", line 182, in add_column
    operations.impl.add_column(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~^
        table_name,
        ^^^^^^^^^^^
    ...<5 lines>...
        **kw,
        ^^^^^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\ddl\impl.py", line 392, in add_column
    self._exec(
    ~~~~~~~~~~^
        base.AddColumn(
        ^^^^^^^^^^^^^^^
    ...<6 lines>...
        )
        ^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\alembic\ddl\impl.py", line 256, in _exec
    return conn.execute(construct, params)
           ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 1421, in execute
    return meth(
        self,
        distilled_parameters,
        execution_options or NO_OPTIONS,
    )
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\sql\ddl.py", line 188, in _execute_on_connection
    return connection._execute_ddl(
           ~~~~~~~~~~~~~~~~~~~~~~~^
        self, distilled_params, execution_options
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 1532, in _execute_ddl
    ret = self._execute_context(
        dialect,
    ...<4 lines>...
        compiled,
    )
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 1848, in _execute_context
    return self._exec_single_context(
           ~~~~~~~~~~~~~~~~~~~~~~~~~^
        dialect, context, statement, parameters
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 1988, in _exec_single_context
    self._handle_dbapi_exception(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        e, str_statement, effective_parameters, cursor, context
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 2365, in _handle_dbapi_exception
    raise sqlalchemy_exception.with_traceback(exc_info[2]) from e
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\base.py", line 1969, in _exec_single_context
    self.dialect.do_execute(
    ~~~~~~~~~~~~~~~~~~~~~~~^
        cursor, str_statement, effective_parameters, context
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\USUARIO\Desktop\stock ultimate\.venv\Lib\site-packages\sqlalchemy\engine\default.py", line 952, in do_execute
    cursor.execute(statement, parameters)
    ~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
sqlalchemy.exc.ProgrammingError: (psycopg2.errors.UndefinedTable) no existe la relación «companies»

[SQL: ALTER TABLE companies ADD COLUMN payment_alias VARCHAR(120)]
(Background on this error at: https://sqlalche.me/e/20/f405)

Command exited with code 1
```

## 3) Diagnostico exacto (archivo, linea, causa, tabla/columna/constraint/indice)

- Archivo: `migrations/versions/20260712_02_hardening_numeric_money_columns.py`
- Linea de fallo: llamada a `op.add_column("companies", ...)` dentro de `_add_company_qr_columns`.
- Causa: la migracion asume que existe tabla `companies` al correr sobre DB totalmente vacia.
- Tabla que dispara el error: `companies`.
- Columna que se intenta agregar al fallar: `payment_alias`.
- Constraint/indice implicado en la falla inicial: ninguno (falla en `ALTER TABLE ... ADD COLUMN`).

## 4) Correccion aplicada (exclusiva de la migracion que falla)

Se corrigio solo la migracion `20260712_02_hardening_numeric_money_columns.py` para que, antes de alterar columnas, garantice esquema base si falta en DB vacia.

Cambio aplicado:

- Se agrego `_ensure_baseline_schema(bind)`.
- En `upgrade()`, se ejecuta `_ensure_baseline_schema(bind)` antes de `_add_company_qr_columns(bind)`.
- `_add_company_qr_columns(bind)` ahora retorna sin operar si `companies` no existe.

Con esto, `flask db upgrade` deja de detenerse en `20260712_02` sobre PostgreSQL vacia.

## 5) Confirmacion de cadena Alembic completa

Luego de corregir `20260712_02`, se re-ejecuto `flask db upgrade` en PostgreSQL vacia y la cadena avanza hasta head.

Ejecucion registrada:

- `20260712_01`
- `20260712_02`
- `20260713_03`
- `20260713_04`
- `20260713_05`
- `20260714_01_referral_program`
- `20260714_02_landing_testimonials`
- `20260714_03_referral_commissions_note`
- `20260714_04_ensure_users_must_change_password`
- `20260714_05_enforce_users_must_change_password`

## 6) Verificacion SQL solicitada

Consulta ejecutada en PostgreSQL local vacia (tras upgrade completo):

```sql
SELECT column_name
FROM information_schema.columns
WHERE table_name='users'
AND column_name='must_change_password';
```

Resultado:

- Devuelve exactamente 1 fila: `must_change_password`.
