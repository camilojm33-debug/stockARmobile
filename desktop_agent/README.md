# StockArMobile — agente local de impresión

Este agente es opcional y se instala únicamente en la PC del comercio. No reemplaza ni modifica el flujo web existente.

## Qué resuelve

- Impresoras térmicas USB instaladas en Windows.
- Impresoras térmicas ESC/POS por Ethernet/TCP (puerto 9100 por defecto).
- Detección de impresoras Windows mediante `GET /printers`.
- Estado del agente mediante `GET /health`.
- Impresión RAW ESC/POS mediante `POST /print`.

## Instalación en Windows

1. Instalar Python 3.11+.
2. Abrir PowerShell en `desktop_agent`.
3. Ejecutar `py -m pip install -r requirements.txt`.
4. Conectar e instalar la impresora térmica normalmente en Windows.
5. Ejecutar `py local_print_agent.py`.
6. Verificar `http://127.0.0.1:8765/health`.
7. Consultar `http://127.0.0.1:8765/printers` para obtener el nombre exacto de la impresora.

## Configuración opcional

Variables de entorno:

- `STOCKAR_PRINT_PORT=8765`
- `STOCKAR_PRINTER_NAME=Nombre exacto de Windows`
- `STOCKAR_PRINTER_HOST=192.168.1.50`
- `STOCKAR_PRINTER_PORT=9100`

## Seguridad

El agente escucha solo en `127.0.0.1` por defecto. No debe exponerse directamente a Internet.

## Ejemplo de impresión

`POST /print` con JSON:

```json
{
  "printer_type": "windows",
  "printer_name": "EPSON TM-T20III",
  "ticket": {
    "brand": "Mi Comercio",
    "sale_id": 520,
    "date": "2026-08-25 23:43",
    "customer": "Consumidor final",
    "payment_method": "Efectivo",
    "items": [
      {"name": "machimbre", "quantity": 1, "unit_price": 5600, "total": 5600},
      {"name": "coca", "quantity": 2, "unit_price": 5300, "total": 10600}
    ],
    "subtotal": 16200,
    "discount": 0,
    "surcharge": 0,
    "tax": 0,
    "total": 16200
  }
}
```
