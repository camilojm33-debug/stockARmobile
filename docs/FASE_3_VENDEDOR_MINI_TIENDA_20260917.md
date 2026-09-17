# Fase 3 — Mini tienda visual del Vendedor IA

## Objetivo
Convertir la publicación `/vendedor/<slug>` en una experiencia de compra híbrida: catálogo real + carrito real + chat del Vendedor IA + preparación de pedido para Mercado Pago.

## Flujo

`Cliente → catálogo → carrito → Vendedor IA (opcional) → preparar pedido → Mercado Pago → webhook existente → confirmación backend`

## Alcance de esta fase

- Catálogo público por comercio con precio, stock, categoría, marca y foto cuando exista.
- Búsqueda de productos por nombre, código, marca o categoría.
- Carrito persistido dentro de la conversación pública del comercio.
- Agregar productos desde el catálogo y quitar líneas del carrito.
- Subtotal/total y unidades visibles.
- Nombre y teléfono opcionales para preparar el pedido.
- CTA a Mercado Pago reutilizando `VendorOrderService`.
- El chat sigue funcionando como alternativa para descubrir productos y modificar el carrito.
- Respuestas del chat devuelven el estado actual del carrito y el link de pago cuando corresponde.

## Reglas

- Nunca usar precio o stock enviados por el navegador para calcular el pedido.
- Toda operación de carrito valida `company_id` y la conversación del visitante.
- El pedido sigue siendo pendiente hasta la confirmación de pago del backend.
- Mantener idempotencia del chat, aislamiento por comercio y publicación estable de Fase 2.
- WhatsApp continúa siendo opcional.

## Fuera de alcance

- Cambios al motor `AgentRuntime` de herramientas comerciales.
- Confirmación de venta desde el frontend.
- Stock descontado desde el navegador.
- Analítica comercial avanzada.
- Rate limiting específico de botones del catálogo; queda como endurecimiento posterior.
