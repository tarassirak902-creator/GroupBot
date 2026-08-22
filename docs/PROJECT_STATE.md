# Project state

## Current stage

Bootstrap v0.1.

## Implemented

- Repository and deployment directory prepared.
- Docker-based Python 3.12 runtime.
- Dedicated PostgreSQL 17 container.
- Environment-based secret configuration.
- Async SQLAlchemy connectivity check.
- aiogram polling bootstrap with `/start`.

## Not implemented yet

- Alembic migration environment and initial schema.
- Multi-group registration and settings.
- RP module.
- XP / levels / achievements.
- Economy and gifts.
- Auto-activity scheduler.
- Moderation/filter sets.
- Audit and moderation logs.
- Production deployment automation.

## Next step

Create Alembic migrations and the first core entities: `groups`, `users`, `group_users`, plus update idempotency storage.
