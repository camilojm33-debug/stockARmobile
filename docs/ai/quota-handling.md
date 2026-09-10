# Manejo de cuota del proveedor IA

StockArMobile diferencia entre un 429 transitorio y el agotamiento de cuota diaria del proyecto.

- Los 429 transitorios pueden reintentarse una vez con un retraso acotado.
- Las cuotas diarias del proyecto no se reintentan inmediatamente.
- El cliente recibe un mensaje seguro y no se exponen trazas ni detalles internos del proveedor.
- El endpoint del chat registra el fallo como warning operativo en lugar de emitir una excepción esperada como traceback.
