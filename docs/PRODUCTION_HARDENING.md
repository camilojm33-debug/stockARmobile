# Production hardening

## Implemented

- WhatsApp deterministic vendor commands are subject to the Vendedor AI plan gate and successful deterministic turns are persisted for auditability.
- Added `/health` as a lightweight database-backed health endpoint. It returns HTTP 200 only when the application can execute a basic database query and HTTP 503 otherwise.

## Operational follow-up

Configure the Render web service health-check path as `/health`. The application endpoint is intentionally database-backed so a process that is alive but cannot reach its database is not reported as healthy.

## Next hardening stages

These remain separate to keep production changes small and reversible:

1. Durable/idempotent WhatsApp webhook processing and explicit transient-error retry semantics.
2. Indexed WhatsApp connection lookup instead of searching encrypted credentials inside tenant preferences.
3. Atomic/daily AI usage projection for high-concurrency workloads.
4. Explicit system actor metadata for AI-created pending orders instead of selecting a fallback admin user.
5. Multilevel referral commission model with auditable parent/child relationships and explicit SuperAdmin overrides.
