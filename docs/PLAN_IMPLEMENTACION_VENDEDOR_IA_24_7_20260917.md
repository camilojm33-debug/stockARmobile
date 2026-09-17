# Plan de implementación — Vendedor IA 24/7

Fecha: 2026-09-17  
Base: `hardening/vendor-24-7-webchat` (`dcc5487a21b1ae0aca4af827a83a0062fa09fd16`)  
Rama de trabajo: `feature/vendor-config-phase1`

## Objetivo

Convertir el Vendedor IA existente en un canal comercial público 24/7 que pueda vender sin depender de WhatsApp ni de un proveedor de mensajería, usando el Webchat público y Mercado Pago, manteniendo aislamiento estricto por comercio y permitiendo que **cada comercio configure su propio Vendedor IA**.

El trabajo se realizará por fases y no se desplegará a producción hasta cerrar la validación E2E y de seguridad de cada fase.

## Requisito funcional nuevo: configuración por comercio

Cada empresa debe poder configurar su Vendedor IA de manera independiente. La configuración pertenece exclusivamente al `company_id` y no puede afectar ni ser visible para otro comercio.

### Configuración mínima del Vendedor IA

- Activar/desactivar el agente.
- Nombre visible del vendedor.
- Personalidad: profesional, amigable, directo o comercial.
- Idioma.
- Instrucciones propias del comercio.
- Información del comercio: productos, envíos, formas de pago, zonas de atención, etc.
- Horario de atención configurado por el comercio.
- Mensaje fuera de horario.
- Permitir recomendaciones.
- Permitir alternativas.
- Permitir preparar presupuestos.
- Permitir tomar pedidos.
- Permitir seguimiento/follow-up cuando esa capacidad esté habilitada por la fase correspondiente.
- Permitir derivación a una persona cuando esa capacidad esté habilitada por la fase correspondiente.
- Publicar/despublicar el Webchat público.
- Compartir enlace público y QR.
- Regenerar/revocar el enlace público.

### Regla de seguridad

La configuración del comercio es dato de tenant. El modelo nunca debe recibir `company_id` desde instrucciones del cliente ni desde el navegador como fuente de autoridad. El backend debe resolver y fijar el `company_id` antes de cargar configuración, herramientas, carrito, cotizaciones o pagos.

## Auditoría actual

### 1. Webchat público

**Estado: implementado en base, pendiente de producto completo.**

Existe un Webchat público opt-in por empresa con token firmado, visitante por sesión, validación de empresa/agente/canal, límite de 500 caracteres y rate limit. El endpoint público reutiliza `AgentRuntime` y `VendorOrderService`.

Pendiente: URL pública estable de largo plazo, publicación/revocación administrable, QR, compartir y experiencia comercial completa.

### 2. Motor del Vendedor

**Estado: base sólida.**

El runtime ya dispone de herramientas para búsqueda de productos, stock, carrito y preparación de pedido. `VendorOrderService.create_pending_order` crea `Quote`/`QuoteItem`, vuelve a comprobar stock y solicita una preferencia de checkout de Mercado Pago. El backend devuelve `payment_url` y `quote_url`.

Pendiente: cerrar el recorrido completo desde Webchat hasta webhook de pago aprobado y venta final sin depender de WhatsApp.

### 3. Configuración por comercio

**Estado: parcial; existe infraestructura pero no debe considerarse cerrada.**

Ya existe `AgentConfiguration` por `agent_id + company_id` y el panel administrativo guarda configuración de Vendedor. También existe `vendor_options` dentro de preferencias de la empresa con personalidad, nombre, saludo, horario, mensaje fuera de horario, información de negocio y flags de capacidades.

Hallazgo clave de auditoría: parte de `vendor_options` actualmente se persiste en preferencias, pero el runtime usa directamente `AgentConfiguration.system_prompt` para las instrucciones y no toda la configuración de `vendor_options` está aplicada de forma explícita al comportamiento. La siguiente fase debe **conectar cada ajuste guardado con el runtime de forma determinista**, sin permitir que una personalización sobrescriba las reglas de seguridad del agente.

### 4. Estado de WhatsApp en administración

**Hallazgo:** la pantalla administrativa histórica todavía trata la ausencia de WhatsApp como "Configuración pendiente" para el Vendedor.

Esto quedó desalineado con el nuevo requisito de Webchat independiente de WhatsApp. Debe corregirse para que:

- Webchat público pueda mostrar al Vendedor como operativo sin WhatsApp.
- WhatsApp sea un canal adicional opcional.
- El estado general del Vendedor no dependa de Meta si Webchat está habilitado.

### 5. Frontend público

**Estado: MVP textual.**

La página pública actual renderiza mensajes como texto. No hay todavía tarjetas de producto, carrito visual, subtotal/total ni CTA de pago dedicado.

### 6. Seguridad multi-tenant

**Estado: buenas barreras iniciales; falta auditoría exhaustiva.**

Hay validaciones de `company_id` en búsqueda de productos, stock, conversación y creación del pedido. La siguiente fase debe probar IDOR de extremo a extremo sobre conversación, carrito, quote, payment, cliente y webhook.

### 7. Idempotencia

**Estado: implementado en chat.**

El runtime ya detecta reintentos por `idempotency_key` y puede devolver la respuesta original sin invocar nuevamente al proveedor. Falta extender la validación de idempotencia al checkout y todos los pasos de confirmación de pago relevantes.

### 8. Rate limiting y costos

**Estado: primera barrera implementada.**

Existe un límite básico por empresa/IP con Redis y fallback por sesión. Falta una política específica para proteger al comercio ante automatización, consumo excesivo del proveedor IA y concurrencia pública.

## Fases

### Fase 0 — Auditoría y contrato técnico

Entregables:

1. Este documento.
2. Matriz de riesgos y criterios de salida.
3. Mapa de rutas, modelos y servicios involucrados.
4. Revisión de CI, migraciones y despliegue.

Criterio de salida: no tocar `main` ni producción; la arquitectura de la siguiente fase queda definida.

### Fase 1 — Configuración real por comercio

Objetivo: que la configuración de cada comercio tenga efecto real en el Vendedor.

Implementar:

- Un único servicio de configuración del Vendedor por `company_id`.
- Validación/normalización de todos los campos.
- Aplicación de personalidad, nombre, saludo, horario, información comercial y capacidades al runtime.
- Separación entre instrucciones personalizadas del comercio y guardrails de seguridad del sistema.
- Estado operativo independiente de WhatsApp.
- Auditoría de cambios de configuración.
- Tests de aislamiento y persistencia.

Criterio de salida: cambiar la configuración del comercio A no cambia ninguna respuesta, agente o configuración del comercio B; y los cambios de A son visibles para su Vendedor en el siguiente turno sin reiniciar el servicio.

### Fase 2 — Publicación del Vendedor

Implementar:

- Identificador público persistente.
- Publicar/despublicar.
- Revocar/regenerar enlace.
- QR.
- Copiar/compartir.
- Vista previa.
- Estado de publicación.

Criterio de salida: un comercio puede publicar su Vendedor sin WhatsApp y compartirlo como enlace o QR.

### Fase 3 — Mini tienda conversacional

Implementar:

- Respuesta estructurada de productos.
- Tarjetas de producto.
- Agregar/quitar del carrito desde UI.
- Carrito visible.
- Cantidades.
- Subtotal/total.
- Manejo de stock insuficiente.
- Confirmación antes de crear pedido.

Criterio de salida: el cliente puede completar la etapa de carrito desde Webchat sin escribir comandos especiales.

### Fase 4 — Checkout completo sin WhatsApp

Validar y endurecer:

- creación de `Quote` y `QuoteItem`;
- Mercado Pago;
- `payment_url`;
- webhook firmado;
- idempotencia;
- confirmación de pago;
- conversión a venta;
- descuento de stock;
- estados de error/cancelación/expiración.

Criterio de salida: venta completa desde cliente público → Mercado Pago → webhook → estado final, sin WhatsApp.

### Fase 5 — Métricas y atribución

Registrar de forma trazable:

- visitas;
- conversaciones;
- productos consultados;
- carritos;
- pedidos;
- pagos;
- ventas atribuidas al Vendedor;
- monto vendido;
- conversión por canal.

Criterio de salida: las métricas de Vendedor no se confunden con ventas globales del comercio.

### Fase 6 — Seguridad y abuso

Pruebas y endurecimiento:

- IDOR multi-tenant;
- token manipulado;
- conversation ID cruzado;
- quote/payment cruzado;
- abuso de rate limit;
- consumo/costo de IA;
- concurrencia;
- replay de webhook;
- reintentos de checkout;
- sesión pública.

Criterio de salida: ningún actor público puede acceder a datos o recursos de otro comercio, ni crear duplicados por retry.

### Fase 7 — UI móvil y despliegue

- experiencia responsive;
- accesibilidad básica;
- estados de carga/error;
- pruebas en móvil;
- CI completo;
- revisión Alembic;
- smoke test de producción;
- compra controlada de prueba.

## Flujo comercial objetivo

```text
Cliente
  ↓
Webchat público del comercio
  ↓
Vendedor IA configurado por ese comercio
  ↓
Productos / precio / stock
  ↓
Carrito
  ↓
Pedido pendiente
  ↓
Mercado Pago
  ↓
Webhook
  ↓
Pago aprobado
  ↓
Venta confirmada + stock actualizado
```

WhatsApp queda como canal adicional. No debe ser requisito para este flujo.

## Principios que no se deben romper

1. Un comercio no puede acceder ni modificar datos de otro comercio.
2. El prompt del comercio nunca puede desactivar reglas de seguridad del sistema.
3. El Vendedor no debe afirmar stock, precio o pago sin evidencia backend.
4. Pedido pendiente no es venta confirmada.
5. Reintentar una operación no debe duplicar pedido, pago ni uso de IA.
6. El Webchat público no debe requerir proveedor de mensajería.
7. La configuración de cada comercio debe ser independiente y auditable.
8. No crear un segundo motor de IA: reutilizar `AgentRuntime` y los servicios comerciales existentes.
