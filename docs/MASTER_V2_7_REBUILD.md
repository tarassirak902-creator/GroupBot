# GroupBot rebuild against current MASTER specification

This branch is a clean rebuild. The production branch `bootstrap/v0.1` is intentionally left untouched until a cutover milestone is ready.

## Source of truth

The current uploaded MASTER document is the only functional/UX source of truth. Confirmed commands, button names, fixed texts and numeric values are not renamed or replaced. Values and syntax that are not explicitly approved remain configurable or unimplemented until approved.

## Architectural rules

- Private chat: owner/account management, groups, networks, advertising, tariff, support, creator panel.
- Group chat: participant commands, moderation, statistics, games, RP and social interactions.
- Main group features and protection modules are enabled, disabled and configured only from the selected group's private owner settings. Once saved there, the setting immediately applies to that group; no additional `+feature`/`-feature` command in the group is required or allowed.
- Group chat commands are runtime actions and queries (for example moderation, profiles, statistics, games and social commands), not a second settings layer.
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

Phases 1–6 are implemented in `src/groupbot`, including group lifecycle and owner management, moderation and protection, statistics, networks, network administrators, and network moderation. The next planned rebuild stage is Phase 7: tariffs, subscriptions, addons, and expiry lifecycle.

The old `app/` package and old Alembic chain remain outside the active new Docker build and are not used by the rebuild runtime.
