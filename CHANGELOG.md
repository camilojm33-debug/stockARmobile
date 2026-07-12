# CHANGELOG - StockArmobile SaaS Audit

## 2026-07-09

- Se trabajo sobre una copia completa del proyecto, sin modificar el original del Escritorio.
- Se corrigio HTML invalido en la pantalla de Productos.
- Se modernizo el POS principal en `/ventas/` con buscador instantaneo, categorias horizontales, scanner, grid responsive, carrito lateral y cobro rapido.
- Se mejoro el carrito JavaScript con totales dinamicos, cambio automatico y sanitizacion de contenido.
- Se endurecio el buscador Spotlight para evitar inyeccion de HTML desde resultados de API.
- Se corrigio el dashboard: IDs duplicados de canvas, graficos Chart.js operativos y bloque de stock critico accionable.
- Se optimizo el dashboard evitando consultas duplicadas de ventas recientes.
- Se agrego filtro real de stock bajo en Productos.
- Se agrego importacion/exportacion Excel de productos con `openpyxl`.
- Se enriquecio Clientes con cantidad de compras y total comprado.
- Se amplio el panel SaaS/Superadmin con MRR, ARR, empresas, usuarios, suscripciones, ingresos, logs y backups.
- Se ajusto PWA para no encolar cualquier POST offline; solo sincroniza endpoints permitidos.
- Se reforzo autenticacion: login case-insensitive, validacion de usuario activo y proteccion contra open redirect.
- Se reforzo configuracion de produccion: `SECRET_KEY` ahora es obligatoria en Render/produccion.
- Se unifico parte de la UI Bootstrap con botones tactiles de 44px y cards mas consistentes.
- Se agrego cobertura smoke para exportacion Excel.

## Verificacion

- Sintaxis Python por AST: OK.
- Sintaxis JavaScript con Node `--check`: OK.
- `pytest -q tests -p no:cacheprovider`: 2 tests OK.

## 2026-07-11

- Se completo la exportacion Excel `.xlsx` para reportes operativos: ventas, compras, gastos, caja, clientes, productos y stock.
- Se agregaron botones Excel en la pantalla de Reportes.
- Se agrego Kardex por producto con movimientos de ventas, compras y modificaciones.
- Se conecto el acceso a Kardex desde el listado de Productos.
- Se agrego exportacion Excel de metricas SaaS/Superadmin.
- Se agrego accion segura para suspender/reactivar empresas desde Superadmin, sincronizando usuarios de esa empresa.
- Se registro auditoria al cambiar el estado de una empresa.
- Se ampliaron tests smoke para reportes Excel, Kardex y metricas Superadmin.

## Verificacion adicional

- Sintaxis Python por AST: OK.
- Sintaxis JavaScript con Node `--check`: OK.
- `pytest -q tests -p no:cacheprovider`: 2 tests OK.
