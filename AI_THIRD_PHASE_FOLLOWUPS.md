# Tercera fase — seguimiento proactivo del Vendedor IA

## Objetivo

La tercera fase agrega un motor de seguimiento proactivo para recuperar compras que quedaron sin terminar y pedidos que quedaron esperando el pago.

El motor **no envía WhatsApp directamente**. Detecta sesiones elegibles y las guarda en `Conversation.metadata_json["ai_outbox"]` para que el conector de WhatsApp las envíe después de configurar Meta/WhatsApp.

## Reglas

- Primer recordatorio: 2 horas sin actividad del cliente.
- Segundo y último recordatorio: 24 horas sin actividad.
- Máximo: 2 recordatorios por ciclo de actividad.
- Un nuevo mensaje del cliente inicia un nuevo ciclo aunque el carrito sea el mismo.
- Si cambia el carrito, cambia la huella del carrito y el ciclo queda desacoplado del anterior.
- Los pedidos pendientes de pago reciben un mensaje específico, no un mensaje genérico de carrito abandonado.
- El recordatorio de 24 horas queda marcado `requires_template=true` para que la futura integración WhatsApp use una plantilla aprobada fuera de la ventana de atención correspondiente.
- No se consulta ni modifica información de otra empresa: cada consulta está acotada por `company_id`.

## Almacenamiento

No se agrega una tabla nueva. Se reutiliza el JSON de la conversación existente:

```text
Conversation.metadata_json.ai_outbox[]
```

Cada item contiene `company_id`, `conversation_id`, destinatario, etapa, texto, huella del carrito, mensaje ancla, estado y una `dedupe_key` determinística.

## Worker

El entrypoint para Render Cron es:

```bash
python run_ai_followups.py
```

El worker es idempotente: al volver a ejecutarse no genera el mismo recordatorio para la misma conversación, mensaje ancla, carrito y etapa.

## Conexión futura con WhatsApp

El conector deberá leer los items `status=pending`, validar nuevamente la conversación/empresa y el ciclo activo, enviar el mensaje (o plantilla cuando `requires_template=true`) y luego llamar a:

```python
AIFollowupService.mark_sent(
    company_id=..., conversation_id=..., dedupe_key=...
)
```

Ante un error permanente puede registrar el fallo con `mark_failed(...)` sin duplicar el recordatorio.
