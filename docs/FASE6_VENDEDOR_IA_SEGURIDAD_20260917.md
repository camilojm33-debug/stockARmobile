# Fase 6 — Seguridad y abuso del Vendedor IA

## Objetivo
Endurecer el canal público para que un visitante sólo pueda operar sobre la conversación y los recursos del comercio resueltos por el backend, incluso ante reintentos, payloads grandes o solicitudes concurrentes.

## Controles incorporados

- El comercio se resuelve exclusivamente por el `slug` público; un `company_id` enviado por el navegador no es una fuente de autoridad.
- Cada mutación pública vuelve a resolver la conversación por `company_id`, canal `webchat` y visitante.
- `order/status`, `order/retry` y `order/cancel` reutilizan la misma conversación tenant-scoped.
- Las mutaciones públicas mantienen un límite de 16 KiB y ahora también se valida el tamaño real del cuerpo cuando no existe `Content-Length`.
- Carrito, checkout, reintento y cancelación pasan por el rate limit público existente.
- Las mutaciones intentan bloquear la fila de conversación con `SELECT ... FOR UPDATE` dentro de la transacción del request, evitando carreras entre operaciones concurrentes sobre el mismo visitante/conversación en bases que soportan row locks.
- Un bloqueo que no puede resolverse no se convierte en una conversación de otro comercio: la validación devuelve una respuesta de acceso inválido.

## Aislamiento de pedido/pago

El flujo comercial existente sigue verificando `company_id` al cargar conversación, cotización, pago y venta. El `external_reference` de Mercado Pago contiene `company_id`, `quote_id` y `conversation_id`, y las transiciones de venta se mantienen en backend.

## Replay e idempotencia

El runtime de chat ya cuenta con idempotencia por `idempotency_key`. Para checkout, `VendorOrderService.create_pending_order` reutiliza un pedido pendiente existente cuando la conversación ya tiene un `pending_quote_id` con un pago pendiente. `retry_payment` también reutiliza un link pendiente antes de crear una nueva preferencia.

La protección de concurrencia de esta fase reduce la ventana de carrera de esos checks. La suite final deberá cubrir además escenarios simultáneos sobre PostgreSQL.

## Criterios de salida

1. No aceptar `company_id` desde el cliente como autoridad.
2. No permitir conversación/quote/payment de otro comercio.
3. No permitir operaciones mutables sin rate limit ni límite de payload.
4. No generar duplicados por retries secuenciales.
5. Serializar mutaciones concurrentes de una conversación cuando el motor de base de datos soporte row locks.
6. Mantener la confirmación de pago y la conversión a venta exclusivamente en backend/webhook.

## Pendiente para la validación final

- Ejecutar `pytest -q` completo.
- Ejecutar CI sobre el conjunto acumulado.
- Verificar específicamente concurrencia real sobre PostgreSQL.
- Hacer smoke test del enlace público y checkout con un comercio de prueba.
