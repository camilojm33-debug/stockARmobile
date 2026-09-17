# Fase 4 — Checkout y cierre comercial del Vendedor IA

## Objetivo
Cerrar el circuito público de compra con estados claros de pedido/pago y controles de idempotencia, sin trasladar al navegador ninguna decisión comercial sensible.

## Alcance

- Estado público del pedido asociado a la conversación.
- Recuperación de un link de pago pendiente cuando exista.
- Reintento de pago usando el servicio existente.
- Cancelación explícita de pedidos todavía no pagados.
- Idempotencia para acciones mutables del checkout.
- Mensajes de estado claros: pendiente de pago, pagado/pendiente de conversión, confirmado e incidencia de pago.
- Mantener la confirmación de venta exclusivamente en el webhook/backend.

## Reglas

- Toda consulta y mutación valida `company_id`, slug publicado y visitante/conversación.
- El cliente nunca puede marcar un pago como aprobado desde el frontend.
- El stock sólo cambia en `VendorOrderService.finalize_paid_order()` después de un webhook de pago aprobado.
- Reintentar pago no crea un segundo pedido si ya existe uno pagable.
- Cancelar requiere confirmación explícita y nunca revierte una venta ya confirmada.
- Las acciones públicas mantienen rate limit y límites de payload.

## Fuera de alcance

- Analítica avanzada o atribución.
- Cambios de proveedor de pagos.
- Nuevos canales de mensajería.
