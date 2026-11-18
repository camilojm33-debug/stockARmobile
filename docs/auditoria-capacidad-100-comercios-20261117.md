# Auditoría complementaria de infraestructura y capacidad — 2026-11-17

## Resumen ejecutivo

Esta pasada cierra una brecha concreta que figuraba en la auditoría anterior: no había una prueba repetible de concurrencia con muchos comercios aislados. Se añade un test de PostgreSQL que crea 100 empresas independientes y ejecuta 100 solicitudes de dashboard sincronizadas, con comprobación de contexto de empresa, usuario y ausencia de marcadores de otros comercios en la respuesta.

También se hace explícita la configuración del pool PostgreSQL. Los valores predeterminados se mantienen en los mismos límites que ya aplicaba SQLAlchemy: 5 conexiones persistentes, hasta 10 conexiones de desborde y 30 segundos de espera. La configuración queda ajustable mediante variables de entorno con rangos validados.

**Importante:** la prueba nueva es un benchmark de concurrencia de aplicación/ORM dentro de CI y una base PostgreSQL efímera. No equivale a una prueba HTTP de extremo a extremo con el servicio Render ni demuestra por sí sola un SLA de producción para 100 comercios. No se ejecutó carga contra producción.

## Evidencia revisada

### Código, integración y CI existentes en `main`

Commit de referencia: `9ab3af6b85e398507ea45e9f8f8c5904b6aa55b5` (17/11/2026).

El pipeline de ese commit terminó correctamente:
- Suite principal: **1.172 pruebas aprobadas, 3 omitidas, 0 fallidas**, en 485,44 segundos.
- PostgreSQL 18: validación de migración mayorista, recuperación/outbox y regresiones de concurrencia de transacciones aprobadas.
- También finalizaron correctamente compilación, auditoría de dependencias, Bandit con el umbral configurado, verificación de una única cabeza Alembic y comprobaciones JavaScript.

La suite principal registró **358 warnings**. No bloquearon la ejecución, pero conviene tratarlos como deuda técnica; en la salida aparecen advertencias de APIs SQLAlchemy legadas y uso de `datetime.utcnow()`.

Las regresiones PostgreSQL actuales cubren concurrencia de compras manuales, confirmación idempotente de facturas IA y serialización del ciclo de vida de ventas. La nueva prueba amplía esa cobertura a 100 contextos tenant simultáneos.

### Estado observado en Render

Servicio web `stockARmobile`:
- Región Oregon; plan Starter; **1 instancia**.
- `gunicorn.conf.py`: 1 worker y 2 threads por defecto.
- Health check configurado en `/readyz`.
- Disco persistente de 5 GB montado en `/var/data`.

PostgreSQL `stockarmobile-prod`:
- PostgreSQL 18; plan Basic 256 MB; **sin réplicas de lectura**.
- Pool administrado de Render: `none`.
- Lista de IP permitidas vacía: la base permanece accesible desde la red privada de Render. No se amplió esa lista para facilitar esta auditoría.

Métricas de Render consultadas para la ventana aproximada 17/11/2026 15:00–21:10, hora argentina, con resolución de cinco minutos:
- Instancias web observadas: 1 durante la ventana.
- Memoria web: máximo observado de 377.954.300 bytes (aprox. 360,5 MiB).
- Conexiones activas de PostgreSQL: entre 1 y 5; máximo observado 5.
- Memoria PostgreSQL: máximo observado de 110.415.870 bytes.
- CPU web y base con valores bajos en la ventana consultada.
- La métrica de latencia HTTP p95 no devolvió puntos de datos. Por ello no existe una línea base fiable de p50/p95 de producción con la que validar un objetivo de respuesta.
- La consulta de logs de aplicación/requests filtrada por warning y error no devolvió eventos en esa ventana.

Los contadores de requests se entregan en intervalos de cinco minutos; no deben interpretarse como requests por segundo ni como capacidad máxima.

### Configuración SQLAlchemy

Se agregan estas variables opcionales:

| Variable | Valor por defecto | Rango permitido |
| --- | ---: | ---: |
| `DB_POOL_SIZE` | 5 | 1–25 |
| `DB_MAX_OVERFLOW` | 10 | 0–25 |
| `DB_POOL_TIMEOUT_SECONDS` | 30 | 1–120 |

El máximo teórico por proceso es `DB_POOL_SIZE + DB_MAX_OVERFLOW`. Con los valores por defecto son 15 conexiones por proceso. El total potencial se multiplica por la cantidad de procesos web; se debe reservar margen para workers, tareas administrativas y otras conexiones. No aumentar workers o pool sin contrastar ese total con `max_connections`, memoria real de PostgreSQL y consumo de la aplicación.

Los valores por defecto no elevan la carga actual ni requieren modificar variables de Render. SQLite conserva su configuración independiente.

## Prueba nueva de 100 comercios

Archivo: `tests/test_tenant_load_capacity.py`.

La prueba:
1. Se ejecuta únicamente contra PostgreSQL real; en SQLite se omite.
2. Crea 100 empresas de prueba, cada una con usuario administrador y membresía activa propia, producto, cliente, venta y detalle de venta.
3. Ejecuta 100 solicitudes a `GET /dashboard/` de 100 tenants distintos, en ráfagas de 20 solicitudes simultáneas, con el pool SQLAlchemy predeterminado (5 conexiones + 10 de overflow, timeout de 30 segundos). Esto cubre 100 contextos aislados bajo una concurrencia acotada y repetible, sin inflar artificialmente el pool solo para CI.
4. Comprueba HTTP 200, empresa y usuario correctos en el HTML y presencia del producto/cliente de ese comercio.
5. Comprueba que no aparezcan marcadores de productos o clientes de otros 99 comercios en la respuesta.
6. Imprime p50, p95, p99 y máximo observados; exige que no se excedan los timeouts configurados y que el pool no conserve conexiones tomadas al finalizar.

La prueba se incorpora al job PostgreSQL de `.github/workflows/ci.yml`, después de las regresiones de concurrencia transaccional. Se puede ejecutar manualmente en un entorno aislado mediante:

```bash
pytest -q -s --tb=short tests/test_tenant_load_capacity.py
```

**Límite de interpretación:** Flask test client no atraviesa TLS, proxy, red, balanceador ni el proceso Gunicorn de Render. El benchmark mide concurrencia de las rutas, la autenticación, las consultas y el pool SQLAlchemy contra PostgreSQL; no es una simulación completa de 100 usuarios externos conectados por Internet. Los números se deben leer como regresión reproducible de CI, no como capacidad comercial certificada.

## Decisiones de seguridad y estabilidad

- No se cambió el plan, número de instancias, número de workers, threads, variables secretas, red privada de PostgreSQL ni configuración de backups/cron.
- No se ejecutaron escrituras ni consultas directas en la base productiva.
- No se habilitó acceso externo a PostgreSQL; el conector de Render no puede consultar una base cuyo allowlist está vacío, y se conservó esa barrera.
- No se ejecutó una prueba de carga contra el servicio público para evitar generar ventas, escrituras, notificaciones, consumo de IA o degradación de comercios reales.
- No se modificaron los flujos de contratación ni pagos del sistema.

## Qué queda para certificar 100 comercios en producción

El benchmark automatizado cubre la concurrencia de aplicación/ORM sobre PostgreSQL aislado. Para afirmar capacidad productiva de 100 comercios aún hace falta ejecutar una segunda etapa en un **staging aislado**, con el mismo tamaño de servicio/base que se pretenda contratar y datos sintéticos:

1. Prueba HTTP real desde fuera de Render con perfiles de 20, 50 y 100 sesiones concurrentes; luego una prueba sostenida de 15–30 minutos.
2. Medición de p50/p95/p99 por ruta, tasa de errores, timeouts, memoria, CPU, conexiones y esperas del pool.
3. Escenarios de dashboard, búsquedas, lectura de productos/clientes, ventas y operaciones de stock; separar endpoints de IA e integraciones externas para no consumir proveedores ni enviar comunicaciones reales.
4. Repetición con datos de volumen representativo y varias sucursales por empresa.
5. Solo entonces decidir, con números, si conviene ajustar pool, threads, plan web o plan PostgreSQL. Antes de escalar procesos deben separarse o coordinarse los dispatchers que se inicializan por worker en `gunicorn.conf.py`.

Como criterios iniciales de aceptación de staging se propone: 100 sesiones tenant simultáneas, cero fugas entre empresas, errores HTTP inferiores al 1%, cero timeouts de conexión/SQL y p95 de dashboard inferior a 2,5 segundos bajo el perfil acordado. Son objetivos propuestos para medir, no resultados ya observados.

## Estado de esta pasada

- [x] Revisar el estado de CI y las regresiones PostgreSQL existentes.
- [x] Inspeccionar configuración y límites observables de Render.
- [x] Hacer explícitos y validables los límites del pool PostgreSQL sin cambiar los defaults.
- [x] Agregar benchmark aislado para 100 empresas y 100 solicitudes concurrentes.
- [x] Integrar el benchmark al job PostgreSQL de CI.
- [ ] Obtener resultado verde del nuevo job y registrar sus percentiles.
- [ ] Ejecutar la etapa HTTP externa en staging para certificar capacidad real del despliegue.

La auditoría general de módulos y rutas debe mantenerse diferenciada de esta prueba de capacidad: una suite verde no sustituye el recorrido E2E visual ni una matriz exhaustiva de autorización/IDOR para cada módulo.
