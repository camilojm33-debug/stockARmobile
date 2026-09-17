# Informe de auditoría y endurecimiento — 2026-09-17

## Esta pasada

- Desacopla el Vendedor IA del requisito de tener WhatsApp conectado para abrir la pantalla.
- Agrega Webchat público opt-in por empresa, con enlace firmado, sesión de visitante, aislamiento por `company_id`, límite de 500 caracteres y rate-limit con Redis cuando `REDIS_URL` está disponible (fallback por sesión).
- Reutiliza el mismo `AgentRuntime` y `VendorOrderService`; no crea un segundo motor IA.
- Añade `Idempotency-Key` a los mensajes del chat autenticado y al webchat público.
- Endurece el prompt del Vendedor para separar canal de inteligencia y para no presentar pedidos pendientes como ventas confirmadas.
- Muestra WhatsApp como canal opcional desde la vista del Vendedor y deja claro que el Vendedor Web no depende de Meta.

## Hallazgos que quedaron para la siguiente pasada específica

- Auditoría E2E completa de Mercado Pago/webhooks y duplicados en todas las rutas de pago.
- Revisión exhaustiva de IDOR en todos los módulos no IA, no solo conversaciones/pedidos IA.
- Conteo real de pedidos/ventas atribuidos al Vendedor por conversación/canal; no se reutiliza el total global de ventas como “ventas derivadas”.
- Revisión final de copy de todos los planes y capacidades contra implementación real de cada módulo.
- Pasada visual móvil global fuera del módulo IA.
- Revisión de migraciones Alembic y despliegue de producción después de validar CI.

## Validación

- No se ejecutó `pytest` localmente desde este entorno.
- El repositorio ya tiene CI en `.github/workflows/ci.yml` que ejecuta compilación Python, validación del service worker y `pytest` en push/PR contra `main`.
- Corrección de autorización: editar campañas IA queda reservado a administradores de la empresa; la transición ya estaba protegida por `company_admin_required`.
- Agregado test de firma/aislamiento del enlace público del Vendedor Web.
- Endurecida la ruta de chat autenticado para incluir siempre el system prompt del agente; el chat público también usa el prompt específico del Vendedor.
- Los reintentos idempotentes ahora devuelven la respuesta del agente ya generada en lugar de una respuesta vacía.
