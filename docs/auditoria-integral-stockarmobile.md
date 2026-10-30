# Auditoría integral de StockArMobile

**Corte:** 2026-10-29
**Revisión:** `main` en `5122226`
**Entorno:** Windows, Python 3.14.4, entorno virtual `.venv`, SQLite en memoria para pytest
**Estado:** dos correcciones de seguridad aplicadas y verificadas localmente; siguen pendientes las verificaciones que requieren PostgreSQL, navegador o credenciales externas.

## A. Resumen ejecutivo

- El árbol estaba limpio y `main` sincronizada con `origin/main` al comenzar; las únicas modificaciones posteriores son las correcciones y pruebas descritas en este informe.
- Inventario registrado: **24 blueprints**, **430 reglas de ruta** (423 endpoints distintos), **76 tablas ORM**, **144 plantillas HTML**, **82 migraciones** y **94 archivos de pruebas** (93 en `tests/` y uno en `desktop_agent/`).
- Línea base antes de corregir: **960 pruebas aprobadas, 0 fallidas**, y 286 advertencias.
- Después de corregir y actualizar `main`: **982 pruebas aprobadas, 0 fallidas**, y **285 advertencias** en 173,75 segundos; incluye 17 pruebas nuevas de regresión.
- Pasaron la compilación Python, los tres chequeos de sintaxis JavaScript y la comprobación de una única cabeza Alembic (`20261028_02_pickup`).
- `pip-audit` no encontró vulnerabilidades conocidas en los requisitos declarados. Bandit no detectó hallazgos de severidad media o alta en el código Python rastreado; señaló 42 hallazgos de severidad baja.
- La revisión manual identificó dos hallazgos confirmados de severidad media: redirección abierta e inyección de fórmulas XLSX. Ambos se corrigieron y se verificaron con pruebas de regresión.
- No se desplegó, no se ejecutaron migraciones en producción, no se enviaron mensajes ni se usaron credenciales externas.

**Resultado general:** las pruebas automatizadas disponibles aprobaron, pero esto no demuestra cobertura funcional completa ni valida UI real, PostgreSQL, Meta/WhatsApp, Mercado Pago, Gmail/SMTP o proveedores de IA en vivo.

## B. Inventario de módulos

Los nombres y cantidades de rutas/tablas se obtuvieron de la aplicación Flask y su metadata en el commit revisado. La cobertura se refiere a pruebas automatizadas existentes y ejecutadas; no equivale a una auditoría de cada ruta ni a E2E de navegador.

| Módulo / blueprint | Función y pantallas identificadas | Datos principales | Pruebas existentes / cobertura observada |
|---|---|---|---|
| `ai_admin` | Administración de agentes y funciones IA desde el área SaaS | agentes, configuraciones, suscripciones IA, auditoría | Pruebas de administración y ciclo de suscripciones; parcial |
| `ai_agents` | Agentes, campañas, facturas, paneles y flujos públicos del vendedor | agentes, conversaciones, mensajes, campañas, destinatarios, facturas | `test_ai_agent*`, `test_ai_marketing*`, `test_vendor_*`; parcial, sin proveedor IA real |
| `auth` | Login, registro, recuperación y restablecimiento de contraseña | usuarios, tokens y solicitudes de recuperación | `test_smoke.py` y pruebas de endurecimiento; cobertura dedicada incompleta |
| `cash` | Caja y movimientos | `cash_sessions`, `cash_movements` | `test_smoke.py`, higiene POS; parcial |
| `clients` | Clientes y formularios/importación | `clients` | `test_client_excel.py`, `test_client_optional_fields.py`, `test_tenant_crm.py`; parcial |
| `company_billing` | Facturación, portal, suscripciones y pagos de empresa | pagos, historial, suscripciones, documentos y conexiones de pago | Pruebas de billing, lifecycle y Mercado Pago; parcial, sin proveedor en vivo |
| `crm` | Oportunidades, actividades, pipeline y ficha de cliente | `crm_opportunities`, `crm_activities`, clientes/leads | `test_tenant_crm.py` y pruebas de campañas/consentimiento; parcial |
| `dashboard` | Paneles comerciales, métricas y cuota IA | ventas, clientes, métricas IA | `test_dashboard_performance.py`, métricas AI; parcial |
| `expenses` | Gastos | `expenses` | Cobertura indirecta en smoke; sin suite específica identificada |
| `maintenance` | Salud, copias, recuperación y tareas operativas | logs de respaldo, auditoría y configuración | `test_backup_*`, readiness, maintenance automation; parcial |
| `products` | Productos, imágenes, códigos, stock y búsquedas | productos, historial de precios/modificaciones | `test_product_images.py`, smoke y pruebas de inventario; parcial |
| `promotions` | Promociones y cálculo de descuentos | promociones, artículos de venta/presupuesto | `test_promotions.py`, regresión de formulario y recibos; parcial |
| `purchases` | Compras, proveedores y pagos de proveedor | órdenes/ítems de compra, proveedores, detalles de pago | `test_purchase_concurrency.py`, smoke; parcial |
| `qr_labels` | Generación de etiquetas QR | productos y datos de etiqueta | Cobertura indirecta; sin suite específica identificada |
| `quotes` | Presupuestos, edición, vista pública/PDF, envío y retiro | presupuestos, ítems, entregas, usuarios/clientes/productos | `test_quote_*`, casos de smoke; parcial, sin E2E visual |
| `referral_network` | Red, comisiones y pagos de referidos | vínculos, comisiones, pagos y vendedores | `test_referral_network.py`, concurrencia de pagos; parcial |
| `referrals` | Activación, panel y recursos de referidos | atribuciones, comisiones y vendedores | Pruebas de referidos; parcial |
| `reports` | Informes y exportaciones | ventas, compras, productos y clientes | Cobertura de paneles/performance y smoke; parcial |
| `saas` | Administración de empresas, planes, CRM comercial, campañas, respaldos y pagos | empresas, planes, leads, campañas, tareas, suscripciones, pagos | Múltiples suites `test_saas_*`, billing y Mercado Pago; parcial |
| `sales` | POS, ventas, tickets y checkout | ventas, ítems, caja, pagos y promociones | `test_sales_services.py`, `test_pos_payment_hygiene.py`, recibos y smoke; parcial |
| `seo_pages` | Landing y páginas públicas SEO | contenido público/configuración | Cobertura de rutas y contenido en smoke; parcial |
| `support` | Tickets y soporte | `support_tickets` | `test_support_flow.py`; parcial |
| `whatsapp_agent` | Webhooks/canales y operación del vendedor IA | conversaciones, mensajes, órdenes, configuraciones | Pruebas de canal, seguridad, recuperación y WhatsApp; parcial, sin Meta real |
| `wholesale_pricing` | Listas mayoristas, reglas por cantidad y aplicación a ventas/presupuestos | listas, ítems, reglas y snapshots de descuentos | `test_wholesale_pricing.py` y regresiones de presupuesto; cobertura amplia de reglas, no de navegador |

### Infraestructura transversal

- Fábrica y configuración Flask: `stockarmobile/`; registro de rutas adicional y modelos heredados: `app.py`.
- Autenticación/permisos: Flask-Login, decoradores de rol/empresa y helpers de alcance por tenant.
- Persistencia: SQLAlchemy/Alembic; SQLite local y PostgreSQL declarado para Render.
- Servicios externos configurables: Mercado Pago, Meta/WhatsApp, proveedores de IA (Gemini/OpenAI/LM Studio), Gmail/SMTP y Redis.
- Procesos programados declarados en `render.yaml`: campañas SaaS e IA, backup, conciliación de facturación y mantenimiento Gmail.
- Agente local de impresión: `desktop_agent/local_print_agent.py`, con instalador Windows y prueba propia (`desktop_agent/test_local_print_agent.py`).
- Scripts operativos adicionales: validación de migraciones, backups, conciliación, Gmail Watch, campañas, diagnóstico de suscripciones y QA local.
- No se encontró un `package.json`, configuración de frontend build, linter o checker de tipos independiente. La UI se entrega principalmente mediante plantillas Flask y JavaScript estático.

## C. Matriz de pruebas y cobertura

| Verificación | Resultado | Evidencia / alcance |
|---|---|---|
| Compilación Python | **APROBADO** | `python -m compileall -q .` con el Python del `.venv` |
| JavaScript | **APROBADO** | `node --check` para `static/service-worker.js`, `static/assets/js/offline-manager-core.js` y `static/assets/js/cart-manager.js` |
| Alembic heads | **APROBADO** | `flask db heads`: una sola cabeza, `20261028_02_pickup` |
| Pytest completo | **APROBADO** | Línea base: 960 passed, 286 warnings. Tras cambios y actualización de `main`: 982 passed, 0 failed, 285 warnings, 173,75 s; SQLite en memoria |
| Dependencias | **APROBADO** | `pip-audit -r requirements.txt`: no known vulnerabilities |
| Análisis Bandit | **APROBADO con observaciones bajas** | Código Python rastreado: 59.880 líneas; 0 issues medios/altos, 42 de severidad baja |
| Upgrade de todas las migraciones en SQLite | **BLOQUEADO por dialecto** | El historial contiene `JSONB` de PostgreSQL; SQLite no puede compilar ese tipo. No indica por sí solo un fallo de producción |
| Upgrade/migración en PostgreSQL | **NO EJECUTADO localmente** | No hay `psql`, `pg_isready` ni Docker disponibles. CI define un servicio PostgreSQL 18 y una validación de migración, pero no se ejecutó esa job aquí |
| UI en navegador / consola / red | **NO EJECUTADO** | No se inició un entorno web de prueba ni se automatizó un navegador |
| Integraciones reales | **NO EJECUTADAS** | Sin credenciales/entornos de sandbox autorizados; los tests locales usan fixtures y mocks |
| Carga/performance | **NO EJECUTADA** | No hay objetivos de rendimiento ni entorno aislado de carga disponible |
| Sintaxis Pylance | **APROBADO** | Sin errores en `strings.py`, `dashboard.py` y `products.py`; `compileall` y pytest validan también el test nuevo |
| Linter / tipos | **NO CONFIGURADO; observaciones** | Pylance reporta `reportUndefinedVariable` en `_save_product_image` y `old_photo` en código preexistente de imágenes no tocado por este cambio |

### Cobertura cuantificada

- Módulos Flask inventariados: **24/24 = 100%**.
- Archivos de pruebas rastreados que se ejecutaron: **94/94 = 100%**.
- Casos pytest después de los cambios y de actualizar `main`: **982/982 = 100%** ejecutados y **982/982 = 100%** aprobados.
- Rutas con prueba individual trazada: **no cuantificado**. No debe inferirse cobertura de los 420 endpoints a partir del conteo de tests.
- Escenarios E2E reales con navegador: **0 ejecutados**. Las pruebas Flask/servicios no verifican renderizado, consola del navegador ni recorrido visual completo.
- Módulos con alguna prueba asociada: el inventario los señala por archivo, pero su profundidad es **parcial** salvo donde se indica suite amplia. La matriz no afirma que cada operación CRUD o cada permiso haya sido probado.

## D. Hallazgos

### SEC-01 — Redirección abierta después del login

- **Severidad:** media (P2) · **Confianza:** 9/10
- **Evidencia:** helper en `stockarmobile/helpers/strings.py:18-42`; callers en `auth.py:192`, `auth.py:638` y `dashboard.py:153-158`.
- **Causa raíz:** la validación original aceptaba cualquier `parsed.path` que comenzara con `/`. `urlsplit()` normaliza `///attacker.example/path` a una ruta que comienza con una sola barra, por lo que el caller podía emitir una redirección ambigua.
- **Impacto:** un enlace de login preparado podría sacar al usuario del sitio después de autenticarlo (phishing/redirección no confiable).
- **Corrección aplicada:** `is_safe_relative_redirect` exige una ruta relativa interna que empiece con una sola `/`, rechaza esquemas, hosts, barras invertidas, controles y separadores iniciales que aparezcan tras decodificar escapes. Login y fin del recorrido guiado usan el helper compartido; el cambio también protege el cambio de sesión que ya lo invocaba.
- **Prueba:** `tests/test_security_regressions.py` reproduce rutas externas, barras múltiples y escapes, además de verificar redirecciones internas válidas en login y tour. Antes de corregir, fallaron las pruebas del helper y las rutas con triple barra; después pasaron.
- **Estado:** **CORREGIDO Y VERIFICADO**.

### SEC-02 — Inyección de fórmulas en exportación XLSX de productos

- **Severidad:** media (P2) · **Confianza:** 8/10
- **Evidencia:** exportadores `products.py:413-445` y `products.py:522-550`; el helper de serialización segura está en `products.py:513-516`.
- **Causa raíz:** los valores de texto se pasaban directamente a Openpyxl; los valores iniciados por `=` se serializaban como celdas de fórmula.
- **Impacto:** al abrir una exportación, una hoja de cálculo puede interpretar como fórmula un nombre controlado por un usuario con permisos de gestión de productos.
- **Corrección aplicada:** `products.py` fuerza las celdas string a tipo texto (`s`) antes de guardar en ambas exportaciones XLSX de productos: catálogo y kardex. Se conservan valores y campos numéricos; la celda no se interpreta como fórmula.
- **Prueba:** `tests/test_security_regressions.py` inspecciona el XLSX generado con prefijos `=`, `+`, `-`, `@`, tab, retorno de carro, salto de línea y espacio, además de una fórmula HYPERLINK en detalle del kardex. Verifica tipo de celda, texto de producto legítimo y valor numérico. Antes de corregir, ambos exports generaban celdas `f`; después los casos pasaron como texto.
- **Estado:** **CORREGIDO Y VERIFICADO**.

### Observaciones no clasificadas como vulnerabilidades confirmadas

- Bandit informó 42 hallazgos de severidad baja; no informó hallazgos medios o altos. Deben revisarse individualmente antes de considerarlos vulnerabilidades.
- Una advertencia de sintaxis de Python señala una secuencia `"\$"` en `tests/test_vendor_order_service.py:310`.
- Hay advertencias deprecadas de `datetime.utcnow()`, `Query.get()` y coerción de subconsultas SQLAlchemy en el conjunto de pruebas.
- Pylance reporta `reportUndefinedVariable` para `_save_product_image` y `old_photo` en código preexistente de carga de imágenes de `products.py`, fuera de las secciones modificadas. No se alteró ese flujo; `compileall` y la suite completa pasan.
- El informe de endurecimiento anterior (`docs/INFORME_AUDITORIA_ENDURECIMIENTO_20260917.md`) ya marcaba pendiente una revisión exhaustiva de IDOR en módulos fuera de IA. Esta pasada no ejecutó una matriz de acceso cruzado para los 420 endpoints.

## E. Flujos de negocio revisados

La suite completa pasó y contiene pruebas para ventas/POS, compras, clientes, presupuestos, precios mayoristas, promociones, recuperación de respaldos, CRM, billing, canales IA, WhatsApp y aislamiento multiempresa en áreas concretas. Esta evidencia es de pruebas automatizadas de servicio/ruta y no equivale a repetir cada proceso desde una UI real.

- **Precios mayoristas:** hay una suite específica extensa (`tests/test_wholesale_pricing.py`) y pruebas de regresión de presupuestos; todos los tests del repo aprobaron. No se ejecutó una sesión manual de navegador ni carga concurrente sobre PostgreSQL.
- **Presupuesto y WhatsApp:** las pruebas cubren la vista pública con número configurado por empresa, normalización `wa.me` y estado sin número (`tests/test_quote_observation_branding.py`). El compartir presupuesto desde el área autenticada es un flujo distinto dirigido al número del cliente; no se modificó ni se debe confundir con el botón público de consulta.
- **Redirecciones:** login y cierre del recorrido rechazan destinos externos/ambiguos y conservan rutas internas legítimas; el helper compartido también endurece el cambio de sesión.
- **Exportaciones XLSX de productos:** catálogo y kardex se inspeccionan tras reabrir el archivo generado; el texto malicioso queda como string y los datos legítimos/números siguen intactos.
- **Vendedor IA/canales:** tests locales ejercitan herramientas, atribución de canal, reintentos y recuperación. No se probaron respuestas reales de Gemini/OpenAI ni entrega real de Meta.
- **Aislamiento de tenant:** existen suites específicas para Mercado Pago, CRM y seguridad del vendedor web, además de casos de smoke. La revisión de todas las rutas/recurso/operación entre dos empresas permanece pendiente.
- **Importación/exportación:** existen pruebas de Excel de clientes y regresiones de importación SaaS; no se recorrieron todos los formatos, tamaños máximos ni escenarios parciales de cada importador/exportador.

## F. Cambios realizados

- `stockarmobile/helpers/strings.py`: valida destinos de redirección internos y rechaza variantes de host externo.
- `dashboard.py`: usa la validación compartida en el parámetro `next` de finalización del recorrido.
- `products.py`: serializa texto como texto en exportación de catálogo y kardex.
- `tests/test_security_regressions.py`: agrega 17 casos de regresión para rutas, login, tour y XLSX.
- `docs/auditoria-integral-stockarmobile.md`: documenta inventario, evidencia, correcciones y límites.
- No se modificaron `auth.py`, dependencias, esquema ni workflows; login ya usaba el helper compartido.
- No se aplicaron migraciones ni se desplegó.

## G. Rendimiento

No se ejecutaron pruebas de carga ni se midieron latencias representativas. Los 173,75 segundos corresponden al tiempo total de pytest y **no** son una medición de rendimiento de la aplicación. No se establecieron objetivos arbitrarios.

## H. Integraciones externas

| Integración | Evidencia local | Validación real |
|---|---|---|
| Mercado Pago | Tests de contrato, seguridad y aislamiento de tenant | No ejecutada |
| Meta / WhatsApp | Tests de parsing, rutas, control de seguridad y mocks | No ejecutada; no se enviaron mensajes |
| IA (Gemini/OpenAI/LM Studio) | Pruebas unitarias con respuestas deterministas/mocks | No ejecutada con proveedores |
| Gmail / SMTP | Pruebas de lógica y fallos simulados | No ejecutada con cuenta o servidor |
| Redis | Configuración/fallbacks en código y tests | No ejecutada contra instancia Redis |
| PostgreSQL / Render | Configuración de despliegue y workflow CI con PostgreSQL 18 | No ejecutado localmente; sin servicio local |

## I. Pruebas pendientes

1. Ejecutar migración y pruebas de recuperación con PostgreSQL aislado (CI o entorno local desechable), incluyendo `scripts/validate_wholesale_migration.py`.
2. Automatizar en navegador los flujos críticos con cuentas ficticias, persistencia tras recarga, consola y errores de red.
3. Completar la matriz CRUD/IDOR de empresa A/B y roles para endpoints privados y recursos compartidos.
4. Ejecutar pruebas de sandbox autorizadas para Mercado Pago y Meta; confirmar estados de entrega del proveedor, no solo recepción HTTP.
5. Recorrer importaciones y exportaciones con archivos válidos, malformados, duplicados y grandes.
6. Revisar las 285 advertencias pytest/deprecaciones; distinguir las advertencias de código de las de dependencias.
7. Definir objetivos de rendimiento antes de ejecutar pruebas de carga en un entorno aislado.

## J. Despliegue

**No realizado.** La configuración declara Render como plataforma y ejecuta migraciones antes de Gunicorn; `render.yaml` también declara workers cron y PostgreSQL. No se inspeccionó ni modificó el dashboard de producción, no se aplicaron migraciones y no se verificaron health checks de producción. No se requiere rollback para esta pasada local.

## K. Riesgos residuales

- SEC-01 y SEC-02 quedaron corregidos y cubiertos por pruebas de regresión; la revisión no ejecutó la UI real ni servicios externos.
- El resultado positivo de pytest no cubre toda la UI, cada endpoint ni la integración real con terceros.
- Las migraciones posteriores a tablas PostgreSQL `JSONB` no se validaron localmente sobre PostgreSQL.
- El conteo global de pruebas puede ocultar áreas sin pruebas de recorrido E2E; el mapa por módulo marca esas áreas como parciales.
- Las advertencias deprecadas pueden convertirse en incompatibilidades futuras, especialmente al subir versiones de Python/SQLAlchemy.

## L. Instrucciones para repetir las verificaciones

En PowerShell, desde la raíz del repositorio:

```powershell
$env:DATABASE_URL='sqlite:///:memory:'
$env:FLASK_ENV='testing'
$env:TESTING='1'
$env:SECRET_KEY='audit-test-only'
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\python.exe -m compileall -q .
node --check static/service-worker.js
node --check static/assets/js/offline-manager-core.js
node --check static/assets/js/cart-manager.js
.\.venv\Scripts\python.exe -m flask db heads
.\.venv\Scripts\pip-audit.exe -r requirements.txt
```

Bandit se ejecutó sobre los archivos Python rastreados, excluyendo pruebas, migraciones y el script QA público:

```powershell
$paths = git ls-files -- '*.py' | Where-Object {
  $_ -notmatch '^(tests|instance|\.git|migrations)/' -and
  $_ -ne 'scripts/qa_vendor_public.py'
}
.\.venv\Scripts\bandit.exe -ll @paths
```

Para validar migraciones, utilizar una base PostgreSQL **desechable** según el workflow `.github/workflows/ci.yml`; no ejecutar el script de validación contra producción.

## M. Recomendaciones priorizadas

1. Ejecutar la job PostgreSQL de CI y documentar el resultado de migración/recuperación.
2. Automatizar recorridos de navegador para login, producto, venta, presupuesto público y administración de precios mayoristas.
3. Ampliar gradualmente la matriz multiempresa al resto de recursos y operaciones.
4. Separar tests de proveedor simulados de pruebas de sandbox reales y mantener ambas evidencias diferenciadas.
5. Atender las advertencias de Python/SQLAlchemy con cambios aislados y pruebas específicas.


## N. Revisión complementaria — redirección en el panel SaaS

**Fecha:** 2026-10-29  
**Rama de revisión:** `fix/saas-safe-redirect-20261029`  
**PR de revisión:** https://github.com/camilojm33-debug/stockARmobile/pull/333

### Hallazgo adicional

La función `_redirect_back()` en `saas.py` aceptaba `next` cuando simplemente comenzaba con `/`. Esa regla acepta destinos ambiguos como `//attacker.example/path`, que algunos navegadores interpretan como una URL externa. Esto dejaba un camino de redirección no seguro en acciones del panel SaaS aunque el login y el recorrido guiado ya usaran el validador compartido.

### Corrección preparada

- `saas.py`: importa y reutiliza `is_safe_relative_redirect()`; ante un destino externo o ambiguo, vuelve al endpoint interno por defecto.
- `tests/test_security_regressions.py`: agrega casos para los valores externos/ambiguos (incluyendo barras múltiples, barras invertidas, codificación y doble codificación URL) enviados tanto por query string como por formulario. También comprueba que una ruta interna legítima siga funcionando.
- La corrección y sus pruebas están en una rama aislada y en un PR draft; no se han fusionado en `main`.

### Estado de validación de esta corrección adicional

- **Inspección de diff remoto:** realizada; el cambio afecta `saas.py`, `tests/test_security_regressions.py` y este informe.
- **Pruebas de regresión de la app:** añadidas, pero su ejecución en CI aún no está confirmada. No se deben presentar como aprobadas hasta obtener los resultados del workflow.
- **Suite general y 285 advertencias:** no reejecutadas en este entorno.
- **PostgreSQL, navegador e integraciones reales:** siguen pendientes, según las limitaciones de las secciones C, H e I.
- **Producción:** no se desplegó ni se modificó Render.

**Evaluación de la corrección adicional:** cambio preparado y aislado; pendiente de ejecución de pruebas automatizadas antes de considerarlo verificado. Este suplemento no cambia el resultado de los tests históricos de `main` descritos en las secciones anteriores.
