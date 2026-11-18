# Motor compartido de plantillas y preflight de campañas

## Objetivo

StockARmobile comparte el motor de reglas, variables, valores predeterminados, bibliotecas de mensajes y resolución posicional de parámetros entre los dos CRM. Los conectores de audiencia, consentimiento, credenciales de WhatsApp y dispatch siguen separados para proteger los límites multiempresa.

## Dos audiencias, dos canales de datos aislados

| CRM | Audiencia | Fuente de contactos | WhatsApp emisor |
|---|---|---|---|
| SuperAdmin comercial | Prospectos que pueden convertirse en comercios de StockARmobile | `SaaSLead` | WABA comercial interna de SuperAdmin |
| CRM del comercio | Clientes de la empresa que inició sesión | `Client` filtrado por `company_id` y la sucursal de campaña | WABA conectada por ese comercio y sucursal |

El motor común vive en `services/campaign_template_engine.py`. No realiza consultas de contactos ni envíos. Cada adapter le entrega campos de su propia audiencia. Por diseño, el motor compartido no mezcla `SaaSLead` con `Client`, y no reutiliza el WhatsApp comercial de SuperAdmin para campañas de comercios.

## Objetivos y bibliotecas de mensajes

SuperAdmin tiene tres objetivos: captación de nuevos comercios, conversión a prueba/registro y seguimiento de prospectos. Su biblioteca comercial contiene propuestas de texto y HTML para esos objetivos.

El CRM de cada comercio tiene cuatro objetivos: promoción general, recuperación de clientes inactivos, fidelización y nuevos productos/novedades. Su biblioteca de WhatsApp maestro se instala en la WABA de cada comercio, con aprobación independiente de Meta para cada plantilla:

- `campana_marketing_es_ar`
- `campana_reactivacion_es_ar`
- `campana_fidelizacion_es_ar`
- `campana_novedad_producto_es_ar`

Las plantillas maestras no reemplazan la aprobación de Meta. Que la plantilla exista en el catálogo local no significa que esté aprobada en una WABA concreta. El preflight vuelve a consultar la cuenta que realmente enviará.

## Variables inteligentes

Cada parámetro posicional de Meta (`{{1}}`, `{{2}}`, etc.) se mapea a un campo permitido para la audiencia correspondiente. Las listas se muestran en el formulario de creación/edición y el servidor vuelve a validar el mapeo.

Campos disponibles para prospectos: contacto, empresa, comercio, teléfono/WhatsApp, email, provincia, rubro, localidad, producto StockARmobile, mensaje, asunto y enlace de prueba.

Campos disponibles para clientes de comercios: contacto, comercio emisor, teléfono/WhatsApp, email, localidad, provincia, tipo de cliente, producto, precio, mensaje y asunto/objetivo.

Los valores de contacto tienen prioridad. Cuando el dato no está disponible se aplica el valor predeterminado de la campaña, siempre que ese campo tenga un valor configurado. Si tampoco existe, el preflight marca el parámetro como faltante y bloquea la aprobación. Nunca se envía un parámetro posicional vacío.

## Preflight

Antes de habilitar aprobación, el CRM revisa, según el canal:

1. Objetivo y alcance de audiencia.
2. Estado del motor y del worker de campañas.
3. Identidad y contenido de la campaña.
4. Configuración del emisor y plantilla aprobada por Meta.
5. Cantidad de parámetros y mapeo, sin duplicados ni campos ajenos a la audiencia.
6. Contactos reales elegibles, permisos/consentimiento y datos necesarios para cada parámetro.
7. Vista previa personalizada con el primer contacto elegible disponible.

Cada bloqueo devuelve el detalle detectado y una indicación de qué campo, conexión, permiso o configuración corregir. La misma validación se repite al preparar destinatarios y en el envío, para evitar que un dato que cambió desde la vista previa pase inadvertido.

## Valores predeterminados

Los valores predeterminados se almacenan en el snapshot de la campaña, dentro de los metadatos de audiencia/segmentación del CRM correspondiente. Son valores de campaña, no alteran fichas de contactos ni se comparten entre empresas.

## Uso operativo

### SuperAdmin

1. Abrir CRM → Campañas de captación.
2. Crear o editar una campaña y elegir objetivo (captación, prueba/registro o seguimiento).
3. Elegir la plantilla WhatsApp aprobada de la WABA comercial cuando el canal incluya WhatsApp.
4. Asignar cada parámetro al campo de prospecto y definir un valor alternativo si la información puede faltar.
5. Elegir una propuesta de la biblioteca y ajustar el asunto/texto/HTML.
6. Guardar, preparar la audiencia y revisar el preflight. Resolver todos los bloqueos antes de aprobar.

### Comercios

1. Conectar y sincronizar el WhatsApp de la sucursal desde Agentes IA → Vendedor IA.
2. Esperar la aprobación de Meta de la plantilla maestra correspondiente al objetivo (Meta revisa cada copia por WABA).
3. Abrir Marketing IA → Campañas y crear un borrador.
4. Elegir objetivo, plantilla de la WABA propia, mapeo de variables y valores predeterminados.
5. Aplicar una propuesta de mensajes si sirve, editarla y revisar la vista previa.
6. En el detalle, ejecutar el preflight y resolver cada bloqueo antes de aprobar.

## Pruebas

Las pruebas cubren el catálogo de objetivos separado por audiencia, la validación de mapeos, la prioridad de datos de ficha frente a defaults, parámetros faltantes, flujo de preparación/envío tenant-scoped y el provisioning idempotente de las cuatro plantillas maestras.

La aprobación de Meta y los estados reales de cada WABA deben verificarse en el entorno conectado: no se presuponen por tener los templates definidos en el repositorio.
