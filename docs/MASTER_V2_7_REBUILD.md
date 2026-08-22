# GroupBot rebuild against current MASTER specification

This branch is a clean rebuild. The production branch `bootstrap/v0.1` is intentionally left untouched until a cutover milestone is ready.

## Source of truth

The current uploaded MASTER document is the only functional/UX source of truth. Confirmed commands, button names, fixed texts and numeric values are not renamed or replaced. Values and syntax that are not explicitly approved remain configurable or unimplemented until approved.

## Architectural rules

- Private chat: owner/account management, groups, networks, advertising, tariff, support, creator panel.
- Group chat: participant commands, moderation, statistics, games, RP and social interactions.
- PostgreSQL is the durable source for critical state and timers.
- Telegram user ID is the identity key; username is display data only.
- Every financial operation uses a wallet plus immutable transaction ledger with unique `transaction_id`.
- Critical actions are written to centralized `audit_log`.
- Telegram update processing is idempotent.
- Group settings are scoped by `chat_id` unless the MASTER explicitly says otherwise.

## Rebuild phases

1. Foundation and group lifecycle: users, groups, owners, members, settings, admin roles/permissions, wallets, transactions, audit, idempotency; pending connection and owner verification.
2. Private owner cabinet and group management screens.
3. Manual moderation, warning scale, punishment history, ranks and permissions.
4. Protection modules: word/phrase sets, anti-flood, anti-spam, anti-link, captcha, newcomers, anti-raid, schedules.
5. User cards, activity/statistics and audience analysis.
6. Networks and network moderation.
7. Tariffs, subscriptions, addons and expiry lifecycle.
8. Advertising marketplace, mandatory subscriptions, creator advertising, reviews/disputes.
9. Game profile, economy, items/inventory/gifts, levels, achievements, tasks and cooldown engine.
10. Cannabis, bottles, growth, duels, fights, relationships/marriages and RP.
11. Auto-messages, reminders, random events, quiet hours and anti-farm.
12. Support, creator panel, diagnostics, broadcasts, final audit and content-library verification.

## Current milestone

Phase 1 foundation has started in `src/groupbot`. The old `app/` package and old Alembic chain are no longer used by the new Docker build and will be removed from this branch after the clean foundation is validated.
