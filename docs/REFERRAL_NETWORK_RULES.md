# Red multinivel de referidos

- Cada vendedor hijo puede tener un único vendedor padre.
- El padre recibe 30% por defecto de cada suscripción referida por ese hijo.
- SuperAdmin puede elevar la comisión del padre a 50% para una relación concreta.
- La comisión normal del vendedor hijo no se modifica.
- No se permiten ciclos ni auto-referidos.
- Las comisiones de red quedan persistidas y vinculadas a la comisión original para auditoría e idempotencia.
- Desvincular un hijo no borra el historial.
- La relación es de un solo nivel padre → hijo para evitar cadenas de comisión difíciles de liquidar.
