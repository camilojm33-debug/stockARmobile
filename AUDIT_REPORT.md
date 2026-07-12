# Informe de Auditoria - StockArmobile

Fecha: 2026-07-09

## Alcance

Se audito la aplicacion Flask StockArmobile sobre una copia de trabajo. El proyecto original ubicado en el Escritorio no fue modificado.

## Arquitectura Detectada

- Aplicacion Flask con SQLAlchemy, Flask-Login, Flask-WTF y Bootstrap 5.
- Modelos y formularios concentrados en `app.py`.
- Blueprints principales: auth, dashboard, products, clients, sales, qr_labels, purchases, cash, expenses, reports y saas.
- Servicios reutilizables: dashboard, busqueda global y notificaciones.
- PWA existente con manifest, service worker e iconos.

## Bugs y Riesgos Corregidos

- HTML invalido en `templates/productos/index.html`.
- Dashboard con `id="categoryChart"` duplicado.
- Graficos del dashboard sin inicializacion.
- Riesgo XSS en toasts del carrito y resultados Spotlight.
- Login sensible a mayusculas/minusculas y sin validar usuario activo.
- Posible open redirect en `next` durante login.
- PWA encolaba cualquier POST offline, incluyendo formularios no sincronizables.
- `SECRET_KEY` de desarrollo podia usarse en produccion si faltaba la variable.
- Filtro `low_stock` enlazado desde notificaciones no funcionaba en Productos.

## Mejoras Implementadas

- POS premium responsive sobre la ruta real `/ventas/`.
- Buscador instantaneo de productos en POS.
- Categorias horizontales en POS.
- Carrito lateral para escritorio y modal de cobro para movil.
- Totales dinamicos, IVA, descuento, recargo y cambio automatico en checkout.
- Excel real para productos: exportacion `.xlsx` e importacion actualizando por codigo.
- CRM con compras y total comprado por cliente.
- Panel SaaS/Superadmin con metricas comerciales y operativas.
- Acciones Superadmin para suspender/reactivar empresas con registro de auditoria.
- Exportacion Excel de metricas globales del SaaS.
- Reportes Excel para ventas, compras, gastos, caja, clientes, productos y stock.
- Kardex por producto con ventas, compras y modificaciones.
- UI tactil con controles minimos de 44px.
- Reduccion de duplicacion visual en CSS del dashboard.

## Seguridad

- CSRF se mantiene activo en formularios.
- SQLAlchemy ORM se mantiene como ruta principal de consultas.
- El uso de SQL textual de migracion esta limitado a tablas/columnas controladas por el codigo.
- Se sanitizan datos de API antes de insertarlos en HTML.
- Produccion exige `SECRET_KEY`.

## Performance

- Se elimino una consulta duplicada de ventas recientes en dashboard.
- Se mantiene cache offline para productos, clientes y ventas recientes.
- Queda como pendiente una optimizacion mayor de agregados del dashboard para bases grandes.

## Pendientes Recomendados

- Separar modelos/formularios desde `app.py` hacia modulos propios cuando se haga una refactorizacion mayor.
- Implementar aislamiento multiempresa completo filtrando por `company_id` en todos los blueprints.
- Crear roles diferenciados `superadmin`, `owner`, `seller` y permisos por modulo.
- Implementar impersonacion segura con logs, vencimiento de sesion y aviso visual antes de habilitarla en produccion.
- Agregar migraciones Alembic formales para reemplazar ajustes automaticos de schema en arranque.
- Agregar pruebas visuales con navegador para POS, dashboard y mobile.
- Reemplazar gradualmente `datetime.utcnow()` por fechas timezone-aware.
