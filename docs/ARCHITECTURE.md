# GroupBot architecture

MASTER-ТЗ v1.0 is the source of functional requirements.

Target layers:

1. Telegram transport / handlers
2. Application services
3. Domain rules
4. Repositories / PostgreSQL
5. Scheduler / workers
6. Content / template provider

Core modules will be separated into RP, progression, economy, activity and moderation. Shared services will cover groups, users, permissions, idempotency and audit logging.

## Bootstrap decisions

- PostgreSQL is the source of truth.
- Redis is intentionally not included in v0.1 because the current VPS has limited RAM and the specification does not require Redis.
- All group-scoped state must be isolated by `chat_id`.
- Numeric values not fixed by MASTER-ТЗ remain configuration, not hard-coded business rules.
- Database schema changes must use migrations.
