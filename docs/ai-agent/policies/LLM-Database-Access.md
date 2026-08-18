# Política: Acceso a bases de datos por Agentes/LLM

Resumen:
- El Agente IA / LLM NUNCA debe acceder directamente a bases de datos, conexiones SQL, drivers, o ejecutar consultas SQL contra PostgreSQL u otros DBMS.
- TODO acceso a datos debe realizarse exclusivamente mediante el *Tool/Action Layer* y los servicios existentes definidos en `services/`.

Reglas obligatorias:
1. Prohibición absoluta: no abrir conexiones a PostgreSQL, no usar `psycopg2`/`asyncpg`/`sqlalchemy` desde componentes del agente ni desde plugins invocados por el agente.
2. Todas las operaciones de lectura o escritura deben exponerse mediante Tools descriptor (`docs/ai-agent/tools/`) o endpoints de servicios autorizados que implementen validación, auditoría y aislamiento por `company_id`.
3. Las Tools que interactúan con la persistencia deben declararse `tenant_scoped: true`, incluir `permissions_required`, y registrar `audit_level` apropiado.
4. No se permiten Tools que reciban y ejecuten SQL arbitrario como parámetro. Cualquier necesidad compleja de consulta debe resolverse en el servicio backend y expuesta como una Tool específica con parámetros tipados.
5. Los eventos y logs deben incluir `trace_id` y `company_id` para rastreabilidad y auditoría.

Responsabilidades de implementación:
- Los desarrolladores deben revisar que ningún Tool acepte un campo `raw_sql` ni parámetros que se pasen directamente a un ejecutor SQL.
- El equipo de seguridad debe auditar la implementación antes de activar `AI_AGENT_ENABLED=true` para cualquier tenant productivo.

Notas:
- Esta política se aplica solo al dominio Agent/LLM y a los componentes que pueda/invoque; no restringe al resto de la aplicación (services/), que debe implementar controles coercitivos.
- Identificador de tenant oficial: `company_id` (usar en todos los nuevos contratos y eventos).