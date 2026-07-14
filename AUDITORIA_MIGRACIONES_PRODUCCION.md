# Auditoria de Migraciones en Produccion (Render)

Fecha: 2026-07-13

## Alcance
- Revision de modelos SQLAlchemy en `app.py`.
- Revision de migraciones Alembic en `migrations/versions`.
- Revision de despliegue en Render (`render.yaml`).
- Foco especial en: `User`, `Company`, `Subscription`, `Referral*`, `Landing*`, autenticacion.

## Nota metodologica
No se realizo introspeccion directa de la base PostgreSQL de Render desde este entorno (no hay acceso directo a la instancia de produccion en esta sesion). El analisis se basa en:
1. Estado de modelos actual.
2. Cadena Alembic del repositorio.
3. Configuracion de despliegue.
4. Errores observados previamente de tablas faltantes en produccion.

---

## 1) Columnas nuevas sin migracion (detectadas)

### 1.1 `referral_commissions.note`
- Modelo: `app.py` (clase `ReferralCommission`), columna `note = db.Column(db.Text)`.
- Migracion esperada: `migrations/versions/20260714_01_referral_program_tables.py`.
- Estado: **NO incluida** en `op.create_table("referral_commissions", ...)`.
- Impacto potencial:
  - Desfase modelo-esquema.
  - Fallos si se intenta persistir/consultar `note` en produccion sin columna.
- Correccion:
  - Crear nueva migracion Alembic que agregue `note` a `referral_commissions`.

---

## 2) Tablas nuevas sin migracion

### Resultado
No se detectaron tablas nuevas de los modulos auditados sin archivo de migracion asociado.

Tablas nuevas y migraciones presentes:
- `support_tickets` -> `20260713_04_support_tickets_and_password_flag.py`
- `password_recovery_requests` -> `20260713_05_password_recovery_requests.py`
- `referral_sellers`, `referral_attributions`, `referral_commissions`, `referral_payouts`, `referral_payout_items` -> `20260714_01_referral_program_tables.py`
- `landing_testimonials` -> `20260714_02_landing_testimonials.py`

---

## 3) Migraciones Alembic faltantes

### Faltante funcional identificado
- Falta migracion para columna `referral_commissions.note` (ver punto 1.1).

### Observacion de consistencia (no bloqueante inmediato, pero importante)
En `20260714_01_referral_program_tables.py` hay diferencias de longitudes/tipos respecto al modelo actual:
- `referral_sellers.dni`: migracion `String(20)` vs modelo `String(30)`
- `tax_id`: `String(32)` vs `String(30)`
- `province`: `String(64)` vs `String(120)`
- `city`: `String(64)` vs `String(120)`
- `bank`: `String(80)` vs `String(120)`
- `account_holder`: `String(120)` vs `String(160)`
- `referral_code`: `String(24)` vs `String(20)`
- `referral_attributions.referral_code`: `String(24)` vs `String(20)`
- `referral_payouts.transfer_number`: `String(80)` vs `String(120)`
- `referral_payouts.observations`: `String(500)` vs `Text`

No necesariamente rompen de inmediato, pero son deuda tecnica de esquema. Recomendado normalizar con migraciones futuras.

---

## 4) Migraciones existentes que no estan siendo ejecutadas

### Hallazgo critico de despliegue
En `render.yaml`:
- `startCommand: gunicorn wsgi:application`

No se ejecuta `flask db upgrade` automaticamente antes de levantar Gunicorn.

Consecuencia:
- Si se despliega codigo con modelos nuevos y no se aplican migraciones manualmente, produccion queda parcialmente migrada.
- Esto explica errores `ProgrammingError` / `UndefinedTable` por tablas faltantes.

---

## 5) Render.yaml ejecuta automaticamente migraciones?

### Respuesta
**No.**

`render.yaml` actual no incluye ejecucion de Alembic en `buildCommand` ni en `startCommand`.

---

## 6) El despliegue necesita ejecutar `flask db upgrade` antes de Gunicorn?

### Respuesta
**Si, obligatorio** para evitar drift esquema-modelo.

Recomendacion operativa minima:
1. Ejecutar manualmente en cada deploy (Render Shell):
   - `flask db upgrade`
2. O automatizar en comando de arranque:
   - `flask db upgrade ; gunicorn wsgi:application`

(En PowerShell local se usa `;`. En Render Linux shell normalmente se usa `&&` o script shell.)

---

## Mapeo solicitado (archivo modelo -> migracion -> correccion)

### User / Authentication
- Modelo: `app.py` (`User.must_change_password`)
- Migracion: `20260713_04_support_tickets_and_password_flag.py`
- Estado: cubierta en repositorio.
- Riesgo produccion: alto si no se corre upgrade.

### Company
- Modelo: `app.py` (`legal_name`, `address`, `phone`, `business_pin_*`, `payment_*`)
- Migraciones: `20260712_02_hardening_numeric_money_columns.py`, `20260713_03_company_profile_and_security_pin.py`
- Estado: cubiertas en repositorio.
- Riesgo produccion: alto si no se corre upgrade.

### Subscription
- Modelo: `app.py` (`subscriptions`)
- Migraciones: no hay cambios nuevos en esta serie para columnas de `subscriptions` (asumidas preexistentes + compatibilidad historica).
- Estado: sin faltantes evidentes en esta auditoria.

### Referral
- Modelo: `app.py` (`ReferralSeller`, `ReferralAttribution`, `ReferralCommission`, `ReferralPayout`, `ReferralPayoutItem`)
- Migracion: `20260714_01_referral_program_tables.py`
- Estado: tabla/base principal cubierta.
- Faltante puntual: columna `ReferralCommission.note` sin migracion.
- Correccion: nueva migracion `ADD COLUMN note`.

### Landing
- Modelo: `app.py` (`LandingTestimonial`)
- Migracion: `20260714_02_landing_testimonials.py`
- Estado: cubierta en repositorio.
- Riesgo produccion: alto si no se corre upgrade.

---

## Plan de correccion recomendado

1. Aplicar migraciones pendientes en produccion (inmediato):
   - `flask db upgrade`

2. Crear migracion faltante para `referral_commissions.note`.

3. (Recomendado) Normalizar diferencias de longitudes/tipos en tablas `referral_*` para alinear 100% con modelos.

4. Ajustar estrategia de deploy para ejecutar migraciones antes de Gunicorn (manual obligatorio o automatizado).

---

## Conclusion
El problema principal de produccion no es la ausencia de Alembic en el repo, sino que **el despliegue en Render no esta ejecutando migraciones automaticamente**. Adicionalmente existe **al menos una columna del modelo sin migracion** (`referral_commissions.note`).

Mientras no se ejecute `flask db upgrade` antes de iniciar la app, la base puede quedar parcialmente migrada y provocar errores por esquema desactualizado.
