# Reconocimiento de facturas

El reconocimiento de facturas es opcional y no cambia el flujo normal de LM Studio.

Configuración reservada para la primera prueba real:

```env
AI_PROVIDER=lm_studio
OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_INVOICE_MODEL=
```

`OPENAI_INVOICE_MODEL` se usa sólo para la extracción multimodal y puede ser distinto de `OPENAI_MODEL`. El proveedor predeterminado sigue siendo `lm_studio`; no se realiza ninguna llamada a OpenAI mientras `AI_PROVIDER` no sea `openai`.

El flujo es:

1. Recibir una imagen JPG/JPEG/PNG/WEBP o PDF en almacenamiento temporal tenant-scoped.
2. Procesar mediante `InvoiceAIService` y el provider seleccionado.
3. Validar nuevamente el JSON y los totales en backend.
4. Resolver proveedor y productos por backend, en orden SKU, barcode, descripción y propuesta aproximada.
5. Mostrar preview y bloquear confirmación si hay ambigüedades o inconsistencias.
6. Confirmar explícitamente para crear/actualizar productos, crear `PurchaseOrder`/`PurchaseItem`, actualizar stock y costo promedio.

Las facturas no se guardan dentro de la conversación: sólo se almacena metadata temporal, extracción normalizada, hash y resultado. El texto del documento se considera DATA y nunca instrucciones del sistema.

## Idempotencia y deuda de modelo

La implementación utiliza `company_id` y SHA-256 del archivo en `Conversation.metadata_json` para evitar reintentos secuenciales y confirmaciones duplicadas. Esta solución no puede imponer unicidad ante dos transacciones concurrentes porque `PurchaseOrder` no tiene columna de referencia externa ni hash único.

Para una garantía SQL fuerte habría que agregar a `purchase_orders` una columna como `source_document_hash` o `external_reference`, un índice único compuesto por `company_id` y esa columna, y una migración Alembic con backfill/validación de duplicados existentes. No se crea esa migración en esta etapa.

`Supplier` no tiene `tax_id`; por eso el matching usa únicamente el nombre existente y no inventa un campo fiscal.

## Primera prueba real futura

Configurar crédito y `OPENAI_API_KEY`, establecer `OPENAI_INVOICE_MODEL`, cambiar `AI_PROVIDER=openai`, subir una factura real, revisar el preview, confirmar y verificar la compra, productos, stock y costo. Esta etapa no ejecuta esa prueba.
