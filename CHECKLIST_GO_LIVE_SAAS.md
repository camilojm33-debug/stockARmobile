# Checklist Go-Live SaaS - StockArmobile

Estado de referencia al 2026-07-16.

## Estado actual

- Codigo base: listo para release candidate.
- Multiempresa: endurecido y validado por pruebas de humo.
- Roles y permisos: separados entre `superadmin`, `admin`, `user`, `seller`.
- Mercado Pago OAuth por empresa: implementado con tokens cifrados.
- Backups por empresa: implementados con validacion de alcance.
- Smoke suite actual: `66 passed`.

## Bloqueantes antes de vender a clientes reales

- [ ] Cargar variables de entorno reales en Render.
- [ ] Confirmar dominio final y `APP_URL` productivo.
- [ ] Ejecutar prueba manual completa sobre PostgreSQL en Render.
- [ ] Verificar webhook real de Mercado Pago en entorno productivo o sandbox estable.
- [ ] Definir y guardar credenciales iniciales de `superadmin` fuera del codigo.
- [ ] Probar restauracion real de backup en ambiente controlado.

## Variables de entorno obligatorias

- [ ] `SECRET_KEY`
- [ ] `DATABASE_URL`
- [ ] `APP_URL`
- [ ] `DEFAULT_SUPERADMIN_PASSWORD`
- [ ] `ADMIN_PASSWORD`
- [ ] `MP_CLIENT_ID`
- [ ] `MP_CLIENT_SECRET`
- [ ] `MP_OAUTH_ENCRYPTION_KEY`
- [ ] `MP_WEBHOOK_SECRET`

## Variables recomendadas

- [ ] `DEFAULT_SUPERADMIN_USERNAME`
- [ ] `DEFAULT_SUPERADMIN_EMAIL`
- [ ] `COMPANY_PIN_SESSION_TTL_MINUTES`
- [ ] `SUPPORT_EMAIL`
- [ ] `SUPPORT_WHATSAPP_DISPLAY`
- [ ] `SUPPORT_WHATSAPP_NUMBER`
- [ ] `SMTP_HOST`
- [ ] `SMTP_PORT`
- [ ] `SMTP_USE_TLS`
- [ ] `SMTP_USER`
- [ ] `SMTP_PASSWORD`
- [ ] `SMTP_FROM_EMAIL`

## Verificacion de despliegue Render

- [ ] `buildCommand` instala dependencias sin errores.
- [ ] `startCommand` ejecuta `python -m flask db upgrade ; gunicorn wsgi:application`.
- [ ] La base PostgreSQL esta conectada correctamente.
- [ ] El deploy arranca sin stacktrace ni fallos de import.
- [ ] El sitio responde por HTTPS.
- [ ] Cookies seguras activas en produccion.

## Verificacion de seguridad

- [ ] Login de `superadmin` funciona solo con credenciales configuradas por entorno.
- [ ] Ningun usuario tenant puede acceder al panel `superadmin`.
- [ ] Un empleado no puede entrar a `Mi Empresa`, compras ni acciones administrativas.
- [ ] Un `admin` no puede ver datos de otra empresa.
- [ ] CSRF activo en formularios normales.
- [ ] Webhook de Mercado Pago responde sin requerir sesion ni CSRF.
- [ ] Los logs no exponen tokens OAuth ni secretos.

## Verificacion funcional minima

### Onboarding

- [ ] Registro de nueva empresa.
- [ ] Login con cuenta admin.
- [ ] Cambio de tema claro/oscuro legible.
- [ ] Apertura de caja.

### Operacion diaria

- [ ] Alta de producto.
- [ ] Alta de cliente.
- [ ] Venta en efectivo.
- [ ] Venta con Mercado Pago QR.
- [ ] Movimiento manual de caja.
- [ ] Cierre de caja con arqueo.
- [ ] Reporte CSV/PDF/Excel.

### Mi Empresa

- [ ] Cambio de datos de empresa.
- [ ] Cambio de PIN.
- [ ] Alta de empleado.
- [ ] Reset de password de empleado.
- [ ] Restriccion correcta de compras a administradores.

### Referidos

- [ ] Captura del codigo `?ref=`.
- [ ] Registro de empresa atribuida al vendedor correcto.
- [ ] No se permite autorreferido.
- [ ] Comision se crea una sola vez.

### Backups

- [ ] Crear backup.
- [ ] Descargar backup.
- [ ] Importar backup de la misma empresa.
- [ ] Rechazar backup de otra empresa.
- [ ] Restaurar backup en ambiente controlado.

## Criterio de salida comercial

StockArmobile queda listo para vender cuando todos estos puntos esten en `OK`:

- [ ] Deploy estable en Render.
- [ ] Variables productivas completas.
- [ ] Mercado Pago validado extremo a extremo.
- [ ] Backup y restore verificados.
- [ ] Superadmin operativo y resguardado.
- [ ] Operacion diaria validada con cuenta admin y cuenta empleado.
- [ ] Sin errores criticos en logs tras 24 horas de observacion.

## Comando de validacion local actual

```powershell
& "c:/Users/USUARIO/Desktop/stock ultimate/.venv/Scripts/python.exe" -m pytest -q tests/test_smoke.py
```

Resultado de referencia actual:

```text
66 passed, 11 warnings
```