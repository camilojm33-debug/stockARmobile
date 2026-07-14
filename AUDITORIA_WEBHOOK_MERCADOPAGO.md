# AUDITORIA FLUJO AUTOMATICO - MERCADO PAGO

Fecha: 2026-07-14
Estado final del flujo: AUTOMATICO Y OPERATIVO (con deduplicacion e idempotencia reforzada)

## 1. Ruta del Webhook

- Ruta: /admin/webhooks/mercadopago
- Archivo: company_billing.py
- Funcion: webhook_mercadopago
- Tipo: POST publico con CSRF exempt (correcto para webhook)

## 2. Ruta que crea la preferencia

- Ruta: /admin/checkout
- Archivo: company_billing.py
- Funcion: create_checkout
- Servicio invocado: BillingService.create_checkout_for_plan

## 3. Funcion que guarda plan seleccionado antes del pago

- Archivo: services/billing_service.py
- Funcion: BillingService.create_checkout_for_plan
- Detalle:
  - Llama a SubscriptionService.start_or_change_plan(...)
  - Deja la suscripcion en estado pending para planes pagos
  - Persiste plan_id y external_reference antes de redirigir a MP

## 4. Funcion que activa la suscripcion

- Archivo: services/webhook_service.py
- Funcion principal: WebhookService.process
- Activacion real delegada en:
  - Archivo: services/subscription_service.py
  - Funcion: SubscriptionService.apply_payment_status

Condicion de activacion:
- payment.status == approved (mapeado a active)

Campos actualizados automaticamente al aprobar:
- status
- start_date
- starts_at
- next_billing_date
- ends_at
- last_payment_date
- renewal_enabled
- auto_renew
- plan (ya guardado antes del pago; se mantiene)

Nota sobre limites/usuarios:
- Los limites (max_users, max_products, max_clients) se derivan del plan asociado (plan_id), por lo que quedan efectivos automaticamente al activarse el plan.

## 5. Funcion que crea la comision por referido

- Archivo: services/referral_service.py
- Funcion: ReferralService.create_commission_for_sale
- Invocacion desde webhook:
  - Archivo: services/webhook_service.py
  - Dentro de WebhookService.process cuando la suscripcion queda activa/aprobada

## 6. Funcion/mecanismo que evita duplicados

Mecanismos activos:
1) Dedupe por evento webhook
- Archivo: services/webhook_service.py
- Funcion: WebhookService.process
- Tabla: webhook_events (event_key unique)

2) Dedupe por pago para comision
- Archivo: services/referral_service.py
- Funcion: ReferralService.create_commission_for_sale
- Regla: si ya existe ReferralCommission para payment_id, no crea otra.

3) Idempotencia en activacion de suscripcion (implementado en esta auditoria)
- Archivo: services/webhook_service.py
- Regla: si llega el mismo payment status para el mismo pago, no reaplica transicion de suscripcion.

4) Correccion de calculo de periodos al aprobar (implementado en esta auditoria)
- Archivo: services/subscription_service.py
- Regla:
  - Primer approved: activa desde ahora
  - Renovacion real: extiende desde next_billing_date solo si el periodo sigue vigente

## 7. Validaciones requeridas

- No se activan pagos pending: OK
- No se activan pagos rejected: OK
- No se activan pagos duplicados: OK
- Webhook repetido no duplica activacion/comision: OK

## 8. Pruebas ejecutadas

Suite completa:
- Comando: python -m pytest -q
- Resultado: 37 passed

Pruebas agregadas en tests/test_smoke.py:
- test_webhook_approved_activates_subscription_and_creates_commission_automatically
- test_webhook_pending_or_rejected_does_not_activate_subscription

Cobertura de estas pruebas:
- activacion automatica al approved
- no activacion para pending
- no duplicacion en reintento del mismo webhook
- creacion automatica de comision cuando hay atribucion de referido

## 9. Confirmacion final

La activacion queda 100 % automatica por webhook de Mercado Pago:
- Se crea checkout y se guarda plan/suscripcion pendiente antes del pago
- Al webhook approved se activa suscripcion sin intervencion manual
- Se genera comision de referido automaticamente cuando corresponde
- Se registra auditoria en el endpoint webhook (record_audit)
- El flujo es idempotente ante reintentos/duplicados
