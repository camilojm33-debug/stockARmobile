# Acceso de una misma cuenta a varias empresas

## Qué cambia

StockArMobile conserva una sola identidad de inicio de sesión por persona y agrega una relación independiente entre esa cuenta y cada empresa. El \`company_id\` histórico de \`users\` se conserva como referencia de compatibilidad; no se reemplaza por la empresa seleccionada en la sesión.

Cada membresía tiene su propio:
- Rol (\`admin\` o \`user\`).
- Conjunto de permisos.
- Estado de acceso.
- Sucursal autorizada.
- Ciclo de invitación y aceptación.

La migración \`20261102_02_company_memberships\` crea \`user_company_memberships\` y backfillea a cada usuario existente desde su empresa actual. Las empresas y datos operativos no se mueven ni se duplican.

## Cómo invitar a una persona

1. Entrar en **Mi Empresa → Empleados**.
2. Ingresar el correo de la persona, el rol y, solamente si todavía no tiene cuenta, un nombre de usuario.
3. StockArMobile detecta si el correo ya existe:
   - **Cuenta existente:** se envía una invitación. La persona inicia sesión con su cuenta habitual y acepta el acceso.
   - **Cuenta nueva:** se crea la identidad y se envía un enlace para establecer su contraseña y aceptar la invitación.
4. La invitación vence a los 7 días. Si el correo no puede enviarse porque SMTP no está configurado, el panel muestra un enlace de un solo uso para compartir manualmente.
5. La empresa aparece en el selector **Cambiar empresa** solamente después de aceptar.
6. Al aceptar, se habilita la membresía y se asigna Casa central. Las otras sucursales deben autorizarse desde **Mi Empresa → Sucursales → Personal habilitado**.

## Roles y permisos por empresa

Una persona puede ser administradora en una empresa y empleada en otra. Los permisos guardados para cada membresía no se mezclan con los permisos de las otras empresas.

La contraseña, el correo y el nombre de usuario pertenecen a la identidad global. Cambiar la contraseña afecta a todas sus empresas. Por eso el panel de una empresa no permite restablecerla manualmente cuando la cuenta tiene más de una membresía; la persona debe usar su recuperación de contraseña o cambiarla desde su cuenta.

## Revocar o volver a conceder acceso

- **Quitar acceso** desactiva solamente la membresía de la empresa seleccionada y elimina las autorizaciones de sucursal de esa empresa.
- La cuenta global y las demás empresas se conservan.
- Una membresía previamente aceptada puede reactivarse por un administrador; se devuelve únicamente a Casa central y las sucursales adicionales requieren una nueva asignación.
- Una invitación pendiente, vencida o cancelada no puede convertirse en acceso activo desde el panel. Debe enviarse una invitación nueva y la persona debe aceptarla.

SuperAdmin separa dos acciones: desactivar una membresía para una empresa o desactivar la cuenta global para todas. Ambas quedan auditadas y protegidas por reautenticación; la reactivación comprueba los cupos del plan.

## Aislamiento y copias de seguridad

La empresa activa se resuelve desde la membresía de la sesión y se aplica como contexto de compatibilidad a los módulos existentes. Ese valor no se persiste en \`users.company_id\`. Las consultas de sucursales, empleados, cuotas, facturación, tareas IA y restauraciones usan la empresa de la membresía.

Los backups representan el rol y los datos de usuario para la empresa respaldada sin duplicar la identidad. Una restauración no reactiva accesos de empleados: las membresías recreadas quedan inactivas y requieren una nueva invitación. Si un ID del backup coincide con el ID de otra persona en la instalación destino, se valida el correo y el nombre de usuario antes de reutilizar la cuenta.

## Configuración de correo

Para enviar invitaciones automáticamente se deben configurar en Render:
- \`SMTP_HOST\`
- \`SMTP_PORT\` (por defecto 587)
- \`SMTP_USER\` (opcional según el relay)
- \`SMTP_PASSWORD\`
- \`SMTP_USE_TLS\` (por defecto habilitado)
- \`SMTP_FROM_EMAIL\` (opcional; valor predeterminado \`no-reply@stockarmobile.com\`)

Sin SMTP, la invitación sigue creándose con un enlace de un solo uso y vencimiento de 7 días; el administrador debe compartirlo manualmente.
