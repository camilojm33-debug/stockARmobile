# Fase 2 — Publicación estable del Vendedor IA

Fecha: 2026-09-17
Base: `feature/vendor-config-phase1`
Rama: `feature/vendor-publish-phase2`

## Objetivo

Permitir que cada comercio publique su propio Vendedor IA mediante una URL pública estable, con QR, compartir, vista previa, despublicación y regeneración del enlace, sin depender de WhatsApp.

## Implementado

- Estado persistente de publicación por comercio dentro de `company.preferences_json`.
- Slug público aleatorio y estable mientras el comercio no lo regenere.
- Publicación y despublicación.
- Regeneración del enlace: el slug anterior deja de resolver al nuevo comercio publicado.
- QR PNG generado por backend como data URI.
- Copiar/compartir el enlace desde el panel de publicación.
- Vista previa administrativa.
- Endpoint público `GET /vendedor/<slug>`.
- Endpoint público de chat `POST /vendedor/<slug>/message`.
- Aislamiento por empresa antes de abrir conversación.
- Reutilización del `AgentRuntime` existente y del rate limit público existente.
- WhatsApp permanece opcional.

## Compatibilidad

El Webchat/token público anterior se mantiene sin reemplazarlo en esta fase, para no cortar enlaces existentes de forma abrupta. La nueva URL estable es la ruta recomendada para publicaciones nuevas.

La revocación/regeneración de la nueva URL estable sí es efectiva: el slug anterior deja de estar publicado y la nueva URL usa otro slug.

## Criterios de seguridad

- No se acepta `company_id` desde el navegador como autoridad.
- El comercio se resuelve por el slug almacenado en las preferencias del tenant y se vuelve a validar contra el registro encontrado.
- La conversación continúa acotada a `company_id`, canal `webchat`, visitante y agente Vendedor.
- El endpoint de chat reutiliza `AgentRuntime`, incluyendo sus controles de plan, herramientas e idempotencia.

## Fuera de alcance

- Mini tienda visual y tarjetas de productos.
- Checkout E2E completo sin WhatsApp.
- Métricas de atribución.
- Despliegue a producción.

## Validación pendiente

La rama se mantiene fuera de `main` y no se despliega desde esta fase. El suite completo debe ejecutarse antes de considerar la fase cerrada. El CI existente apunta a PR contra `main`, por lo que esta rama apilada sobre `feature/vendor-config-phase1` no debe interpretarse como validada por CI automático.
