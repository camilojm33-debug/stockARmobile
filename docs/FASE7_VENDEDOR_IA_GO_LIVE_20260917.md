# Fase 7 — Go-live, móvil y validación del Vendedor IA

## Estado del frontend público

La mini tienda ya cuenta con:

- catálogo responsive de productos;
- buscador por producto, código, marca y categoría;
- carrito visible y barra fija de resumen en móvil;
- chat como alternativa al catálogo;
- datos opcionales de cliente;
- CTA a Mercado Pago;
- mensajes de error y estado;
- escaping de contenido textual y validación de URLs de imágenes antes de renderizar.

## QA seguro

Se incorpora `scripts/qa_vendor_public.py` para comprobar una URL pública sin crear pedidos ni pagos. Valida:

1. página pública;
2. catálogo;
3. estado del visitante;
4. rechazo de una cancelación con conversación inválida.

## Validación final acumulada

Antes de cualquier merge a `main`:

- `pytest -q` completo;
- `python -m compileall -q .`;
- `node --check static/service-worker.js`;
- CI de GitHub Actions sobre el conjunto acumulado;
- smoke QA público con un comercio de prueba;
- prueba manual de carrito → pedido pendiente → Mercado Pago → webhook → venta confirmada;
- prueba manual de aislamiento entre dos comercios;
- verificación de regeneración/despublicación del enlace público.

## Reglas de release

No se despliega una fase parcialmente validada. Las PR de las fases permanecen apiladas y en `main` no se cambia nada durante esta etapa.

La confirmación de pago, la conversión a venta y el descuento de stock continúan exclusivamente en backend/webhook.
