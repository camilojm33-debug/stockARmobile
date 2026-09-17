# Validación técnica fases 1-4

Esta rama acumulada se valida mediante pytest y GitHub Actions antes de cualquier merge a `main`.

## Alcance
- Configuración por comercio del Vendedor IA.
- Publicación estable y QR.
- Mini tienda pública con catálogo y carrito.
- Checkout público y endurecimiento de mutaciones.
- Flujo Mercado Pago → webhook → confirmación backend.

## Regla
No se mergea ni despliega mientras la validación no esté documentada como satisfactoria.
