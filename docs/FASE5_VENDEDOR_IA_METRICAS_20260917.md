# Fase 5 — Métricas y atribución del Vendedor IA

## Objetivo
Registrar y proyectar métricas del Vendedor IA por comercio sin crear una segunda fuente de verdad para pedidos, pagos o ventas.

## Métricas

- Visitas al Vendedor público.
- Conversaciones iniciadas.
- Consultas del catálogo/productos.
- Carritos activos y acciones de carrito.
- Pedidos generados.
- Intentos de pago y pagos aprobados.
- Ventas atribuidas al Vendedor.
- Monto vendido atribuido.
- Conversión global y por canal.

## Atribución

La atribución comercial se relaciona mediante los identificadores ya existentes de `company_id`, `conversation_id`, `quote_id` y las referencias de Mercado Pago. No se duplica el registro de ventas ni se permite que el navegador declare una venta aprobada.

Las acciones de navegación pública se guardan como metadatos de la conversación del Vendedor. Se usa idempotencia para no duplicar el evento de visita o inicio de conversación dentro del mismo visitante/conversación.

## Aislamiento

Todas las consultas de métricas parten del `company_id` activo y vuelven a validar ese comercio al relacionar conversaciones, pagos, pedidos y ventas.

## Alcance de esta fase

- Servicio `vendor_metrics_service.py`.
- Instrumentación de las rutas públicas existentes mediante `after_request`.
- Vista administrativa `/dashboard/ai-agent/vendor-metrics`.
- Acceso directo desde la pantalla de publicación del Vendedor.
- Tests unitarios de idempotencia y extracción de referencias.

## Criterio de salida

Las métricas del Vendedor deben poder consultarse para un comercio sin mezclarse con ventas globales, y cada venta atribuida debe poder seguirse hasta su pedido/conversación mediante los identificadores backend existentes.

## Pendiente para una etapa posterior

Las consultas de productos realizadas exclusivamente por herramientas internas del modelo todavía no se registran como eventos de producto individual; por ahora se contabilizan las consultas del catálogo público y el resultado devuelto por ese catálogo.
