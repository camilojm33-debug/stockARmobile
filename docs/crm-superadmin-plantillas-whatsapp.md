# Plantillas de WhatsApp en el CRM de SuperAdmin

## Alcance

El selector de plantillas está integrado únicamente en el CRM comercial interno de StockARmobile (SuperAdmin). Consulta la cuenta de WhatsApp Business configurada para **StockArMobile Comercial**. No reutiliza credenciales ni plantillas de los WhatsApp de los comercios clientes y no modifica los flujos de sus CRM.

## Uso

1. Desde SuperAdmin, entrá en **CRM → Campañas de captación** y creá una campaña o abrí un borrador para editarlo.
2. Elegí **WhatsApp** o **Email + WhatsApp** como canal.
3. En **WhatsApp Comercial · plantillas de Meta**, esperá la sincronización o pulsá **Actualizar plantillas**.
4. Seleccioná una plantilla compatible. Se muestran nombre, idioma, categoría, estado y una vista previa del cuerpo.
5. El sistema genera un campo por cada variable posicional del cuerpo (`{{1}}`, `{{2}}`, etc.). Asigná a cada una el dato que corresponda.
6. Guardá el borrador, prepará la audiencia y revisá el preflight. La campaña solo se podrá aprobar/enviar cuando la configuración, la audiencia y los permisos por canal sean válidos.

## Compatibilidad de plantillas

Para campañas promocionales el CRM habilita únicamente plantillas con estado `APPROVED`, categoría `MARKETING` y cuerpo de texto compatible. Las variables del cuerpo deben ser posicionales y consecutivas, desde `{{1}}`, con un máximo de diez. Las plantillas con variables en encabezados, pies o botones, o con encabezados multimedia, quedan deshabilitadas porque el envío actual solo completa parámetros del cuerpo.

Las plantillas de categoría Servicio/Utility, pendientes o rechazadas no se pueden usar como campañas de marketing. Una plantilla estática, sin variables, se admite sin parámetros.

## Campos disponibles para asignar

| Campo | Valor enviado |
|---|---|
| Nombre del contacto | Contacto del prospecto |
| Nombre de la empresa / comercio | Nombre del negocio |
| Rubro | Rubro registrado en el prospecto |
| Localidad | Localidad del prospecto |
| Producto | `StockArMobile` |
| Texto de la campaña | Texto de la campaña, sustituyendo variables como `{{contacto}}` |
| Asunto / título | Asunto de la campaña, con variables del prospecto resueltas |
| Enlace para probar | URL de registro de prueba configurada para la aplicación |

La asignación se conserva en el borrador. Al cambiar de plantilla se reconstruye el mapeo para la cantidad de variables de la nueva plantilla. Las plantillas estáticas guardan el valor especial `none`.

## Controles de envío

El CRM vuelve a consultar la plantilla y verifica su estado/categoría y el número de parámetros tanto al preparar/aprobar la campaña como dentro del worker, justo antes del despacho. También se mantienen los controles existentes de consentimiento `opted_in` de WhatsApp, lista de supresión y bloqueo de no contactar. Tener un teléfono o un dato público no representa consentimiento de marketing.

El selector no envía mensajes por sí mismo. Los mensajes salen únicamente mediante el flujo de aprobación y envío de campañas de SuperAdmin. La integración no cambia el CRM de cada comercio.

## Requisitos operativos

El canal interno **StockArMobile Comercial** debe tener un WABA ID, Phone Number ID y autorización de Meta con los permisos necesarios para consultar plantillas. Si el selector no puede cargar el catálogo, revisar primero la conexión desde la configuración **WhatsApp Comercial** de SuperAdmin. No se debe copiar un token de Meta en una plantilla ni en el navegador.
