# AUDITORIA_POSTGRES_RENDER

Fecha: 2026-07-13

## Estado de acceso a PostgreSQL de Render

- `DATABASE_URL` en entorno local: **No definida**.
- Archivo `.env` local con `DATABASE_URL`: **No existe**.

Conclusión: en esta sesión **no fue posible conectarse** a la base PostgreSQL de producción para leer `INFORMATION_SCHEMA` ni `alembic_version` directamente.

No se ocultaron errores: la auditoría incluye evidencia verificable de modelos y migraciones, y deja las consultas SQL exactas para ejecutar en Render Shell.

---

## Cadena Alembic en repositorio (referencia esperada)

Head actual en código:
- `20260714_02_landing_testimonials`

Historial:
- `20260712_01_products_barcode_company_unique`
- `20260712_02_hardening_numeric_money_columns`
- `20260713_03_company_profile_and_security_pin`
- `20260713_04_support_tickets_and_password_flag`
- `20260713_05_password_recovery_requests`
- `20260714_01_referral_program_tables`
- `20260714_02_landing_testimonials`

---

## Tabla comparativa (Modelo vs Migracion vs PostgreSQL real)

| Tabla | Columna | Existe en modelo | Existe en migracion | Existe en PostgreSQL | Estado |
|---|---|---:|---:|---:|---|
| users | must_change_password | Si (`app.py`) | Si (`20260713_04`) | No verificable en esta sesión | Pendiente validar en Render |
| support_tickets | id, company_id, user_id, email, reason, description, status, created_at, resolved_at, resolved_by_user_id, resolved_note | Si (`app.py`) | Si (`20260713_04`) | No verificable en esta sesión | Pendiente validar en Render |
| password_recovery_requests | id, company_id, user_id, email, status, requested_at, processed_at, processed_by_user_id | Si (`app.py`) | Si (`20260713_05`) | No verificable en esta sesión | Pendiente validar en Render |
| referral_sellers | id, user_id, dni, tax_id, phone, province, city, address, alias, cbu, bank, account_holder, referral_code, referral_url, active, created_at, updated_at | Si (`app.py`) | Si (`20260714_01`) | No verificable en esta sesión | Pendiente validar en Render |
| referral_attributions | id, seller_id, company_id, user_id, referral_code, created_at | Si (`app.py`) | Si (`20260714_01`) | No verificable en esta sesión | Pendiente validar en Render |
| referral_commissions | id, seller_id, attribution_id, company_id, subscription_id, payment_id, plan_id, sold_amount, commission_percent, commission_amount, status, created_at, available_at, paid_at, cancelled_at | Si (`app.py`) | Si (`20260714_01`) | No verificable en esta sesión | Pendiente validar en Render |
| referral_commissions | note | Si (`app.py`) | **No** (faltante en `20260714_01`) | No verificable en esta sesión | **Desfase modelo/migracion detectado** |
| referral_payouts | id, seller_id, processed_by_user_id, amount, transfer_date, receipt, transfer_number, observations, created_at | Si (`app.py`) | Si (`20260714_01`) | No verificable en esta sesión | Pendiente validar en Render |
| referral_payout_items | id, payout_id, commission_id | Si (`app.py`) | Si (`20260714_01`) | No verificable en esta sesión | Pendiente validar en Render |
| landing_testimonials | id, author_name, company_name, quote, active, created_at, updated_at | Si (`app.py`) | Si (`20260714_02`) | No verificable en esta sesión | Pendiente validar en Render |

---

## Hallazgos confirmados

1. **Columnas nuevas sin migracion**
- `referral_commissions.note` (modelo presente, migracion ausente).

2. **Tablas nuevas sin migracion**
- No se detectaron tablas nuevas sin archivo Alembic en el repositorio para los modulos auditados.

3. **Migraciones Alembic faltantes (en repositorio)**
- Falta una migracion para agregar columna `note` en `referral_commissions`.

4. **Migraciones existentes no ejecutadas (produccion)**
- No verificable en esta sesión sin acceso a `alembic_version` de Render.
- Debe verificarse con SQL (sección siguiente).

5. **`render.yaml` ejecuta migraciones automaticamente?**
- **No**.
- `startCommand` actual: `gunicorn wsgi:application`.
- No incluye `flask db upgrade`.

6. **¿Se necesita `flask db upgrade` antes de Gunicorn?**
- **Si, obligatorio** para evitar drift entre código y esquema.

---

## SQL exacto para ejecutar en Render (Shell o cliente SQL)

### A) Revision aplicada en produccion
```sql
SELECT version_num FROM alembic_version;
```

### B) Tablas objetivo
```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'users',
    'support_tickets',
    'password_recovery_requests',
    'referral_sellers',
    'referral_attributions',
    'referral_commissions',
    'referral_payouts',
    'referral_payout_items',
    'landing_testimonials'
  )
ORDER BY table_name;
```

### C) Columnas objetivo
```sql
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'users' AND column_name IN ('must_change_password')) OR
    (table_name = 'referral_commissions' AND column_name IN ('note')) OR
    (table_name = 'landing_testimonials') OR
    (table_name = 'referral_sellers') OR
    (table_name = 'referral_attributions') OR
    (table_name = 'referral_commissions') OR
    (table_name = 'support_tickets') OR
    (table_name = 'password_recovery_requests')
  )
ORDER BY table_name, column_name;
```

### D) Comando de migracion recomendado en Render
```bash
flask db upgrade
```

---

## Migraciones que deben estar aplicadas en produccion

Esperado (en orden):
1. `20260712_01_products_barcode_company_unique`
2. `20260712_02_hardening_numeric_money_columns`
3. `20260713_03_company_profile_and_security_pin`
4. `20260713_04_support_tickets_and_password_flag`
5. `20260713_05_password_recovery_requests`
6. `20260714_01_referral_program_tables`
7. `20260714_02_landing_testimonials`

Si `alembic_version.version_num` en Render es menor al head (`20260714_02_landing_testimonials`), hay migraciones pendientes de ejecutar.

---

## Correccion recomendada por diferencia

1. **Migracion faltante de columna**
- Crear nueva migracion Alembic para:
  - `ALTER TABLE referral_commissions ADD COLUMN note TEXT NULL;`

2. **Sincronizacion de esquema en produccion**
- Ejecutar `flask db upgrade` antes de levantar Gunicorn.

3. **Pipeline de deploy**
- Ajustar despliegue para que migre antes de iniciar app:
  - O bien en release/command de Render.
  - O bien en `startCommand` con script de arranque que ejecute upgrade y luego Gunicorn.

---

## Resumen ejecutivo

- Se detecto un desfase confirmado entre modelo y migracion: `referral_commissions.note`.
- El resto de tablas nuevas auditadas tiene migraciones en repositorio.
- El despliegue actual en Render **no ejecuta migraciones automaticamente**.
- Para confirmar diferencias exactas contra PostgreSQL real de produccion, ejecutar las consultas SQL de esta auditoria en Render.
