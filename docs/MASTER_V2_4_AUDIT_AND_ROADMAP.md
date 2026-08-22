# GroupBot — аудит и план реализации по MASTER-ТЗ v2.4

Статус: рабочая карта разработки. Источник истины — MASTER-ТЗ v2.4 (консолидированная полная версия). Более ранние ТЗ используются только как история; при расхождении приоритет у более позднего и более конкретного правила v2.4.

## Обозначения

- ✅ — уже есть рабочая база/проверено в живой группе.
- 🟡 — есть часть механики, но до v2.4 не хватает обязательных правил/UX/данных.
- ❌ — отсутствует как законченный модуль.
- 🔧 — существующую реализацию нужно переделать/расширить, а не просто дописать рядом.

## 1. Фундамент

### ✅ Уже есть
- Python 3.12 + aiogram 3, async architecture.
- PostgreSQL 17, SQLAlchemy async, Alembic migrations.
- Docker runtime на VPS.
- Отдельные данные по chat_id для текущих групповых механик.
- users / groups / group_users.
- обработка активности и last_activity_at.
- processed_updates как базовая защита от повторной обработки update.
- модульная разбивка по router/middleware/service/content.

### 🔧 Нужно довести до v2.4
- централизованный audit_log для критических изменений;
- единый transaction_id/idempotency для всех финансовых и предметных операций;
- owner/group lifecycle: подключена / отключена / ожидает подключения / тариф приостановлен;
- критичные таймеры только в БД;
- единый слой permissions, а не разрозненные проверки Telegram administrator;
- модель текущего членства (active/left/banned/deleted), а не только факт когда-то замеченного пользователя;
- кликабельные mention через telegram_user_id вместо plain full_name во всех системных сообщениях.

## 2. Личный кабинет и UX

### ❌ Отсутствует
Главный личный кабинет владельца:
- 👥 Мои группы
- 🌐 Сетки групп
- 📢 Реклама
- 💳 Тариф и подписка
- 🛠 Поддержка
- 👤 Мой аккаунт
- 👑 Панель создателя (только создатель)

После выбора группы:
- 🛡 Модерация
- 👮 Администрация
- 🤖 Автоматизация
- 📊 Статистика
- 🎮 Настройки развлечений
- 📢 Реклама группы
- ⚙️ Настройки группы

Обязательны иерархические inline-меню, ◀️ Назад и 🏠 Главное меню.

### ❌ Меню группы v2.4
Нужно реализовать Telegram command menu:
/help, /guide, /commands, /games, /profile, /stats, /rules, /support.

## 3. Подключение групп и права бота

### 🟡 Текущий статус
Бот уже видит группы и создаёт данные после активности.

### ❌ Нужно по v2.4
- состояние «бот добавлен, но группа не подключена»;
- команда/действие «подключить» только фактическим владельцем;
- проверка необходимых прав;
- автоматический выход через 1 минуту, если не подключили;
- отключение владельцем + 2 минуты на возврат;
- диагностика прав удаления/банов/мута/закрепления/публикации/updates;
- данные группы сохраняются после отключения/выхода бота.

## 4. Роли и администрация

### 🟡 Есть
- проверка Telegram creator/administrator для части команд.

### ❌ Нужно
- group_owners;
- admin_roles;
- admin_permissions;
- собственные названия рангов;
- индивидуальные права;
- резервный администратор;
- сетевые администраторы;
- безопасная передача владельца с подтверждением и audit log;
- единый PermissionService для всех админских действий.

## 5. Модерация

### ✅ Проверено
- несколько независимых filter_sets;
- word / phrase;
- whole / contains;
- case-sensitive toggle;
- enable/disable;
- delete / warning / mute / ban;
- delete + наказание;
- причина;
- приоритет Ban > Mute > Warning > Delete;
- все совпавшие наборы журналируются;
- whitelist;
- исключение администрации;
- modlog;
- warnings и очистка предупреждений;
- ошибки Telegram API не должны ронять polling (базовая обработка существует).

### 🟡 Требует доведения
- VIP exclusion на уровне каждого набора;
- карточка истории фильтра в утверждённом UX;
- normalized/original match data;
- правило нескольких mute разной длительности оставить конфигурируемым до отдельного утверждения.

### ❌ Ручная модерация v2.4
- бан/мут/пред ответом на сообщение словами без slash-команд;
- режим 1 текст, режим 2 кнопки, режим 3 оба;
- свои причины наказаний и сроки;
- шкала предупреждений 1/5, 2/5, 3/5 → mute 15m, 4/5 → mute 1h, 5/5 → ban, с возможностью настройки в глобальных рамках;
- разбан / размут;
- очистить пользователя;
- бан + очистка;
- «мои баны», «мои муты», «выдал пред»;
- банлист, мутлист, преды;
- «закрепи»;
- punishments как отдельная сущность текущих/исторических наказаний.

### ❌ Защитные модули
- антифлуд;
- антиспам одинаковых/почти одинаковых сообщений;
- антиссылки + allowed domains;
- капча;
- ограничения новичков;
- антирейд;
- расписание усиленной защиты.

## 6. Пользовательская карточка и статистика

### 🟡 Есть база
- users/group_users;
- last_activity_at;
- XP/level/balance.

### ❌ Нужно
- «кто я» / «кто ты»;
- current membership state;
- admin rank;
- marriage partner;
- message stats day/week/month/total;
- deleted messages count;
- first_seen/joined basis without invented Telegram history;
- member-only personal stats;
- admins full group stats;
- message_stats / user_activity aggregation.

## 7. Аналитика аудитории

### ❌ Отсутствует
- VIP / active / Premium / silent / deleted / joined-no-message categories;
- TEST: exact total + category percentages;
- paid: exact numbers + percentages;
- quick analysis min 3h;
- full analysis min 24h;
- manual analysis obeys cooldown;
- explicit UX that Telegram data is observational, not guaranteed full census.

## 8. Тарифы и подписки

### ❌ Отсутствует
- TEST 3 days;
- BASIC <=15k / 1 group;
- STANDARD <=50k / 3 groups;
- PRO <=100k / 10 groups;
- MAX <=200k / 20 groups;
- individual tariff;
- tariff_limits / addons / subscriptions;
- TEST limits from canonical v2.4;
- 5-hour pre-expiry notice;
- expiry → pause → 5-minute renewal window → leave group;
- participant limit warning/grace (not abrupt critical moderation shutdown);
- add-on lifecycle tied to subscription period;
- creator editable limits/prices.

## 9. Сетки групп

### ❌ Отсутствует
- networks / network_groups / network_admins;
- groups only of same owner;
- network stats;
- copy templates/settings;
- сбан / сразбан / сбанлист;
- custom network reasons;
- confirmation for dangerous multi-group actions.

## 10. Правила и приветствие

### ❌ Отсутствует
- welcome text/photo/buttons/name substitution/autodelete;
- per-group rules editor;
- word «правила» and /rules response.

## 11. Экономика

### ✅ Есть база
- group-specific balance;
- transactional /pay and admin grant;
- transaction journal;
- row locking and no negative transfer balance.

### 🔧 Нужно привести к обязательной модели v2.4
- отдельный wallet, а не только group_users.balance;
- immutable ledger transactions;
- unique transaction_id;
- idempotency for rewards/purchases/callback retry;
- standard kinds (DAILY_BONUS, GIFT_PURCHASE, RANDOM_EVENT, JACKPOT, etc.);
- items / inventories / gifts;
- atomic item + money operations;
- creator-editable economy parameters.

## 12. XP / уровни / достижения

### ✅ Есть
- per-group XP;
- configurable xp_per_message and thresholds;
- level up messages;
- achievements / user_achievements;
- unique one-time achievement protection;
- active achievement definitions;
- achievement reward XP/currency logic exists in branch.

### 🔧 Нужно
- canonical default XP formula XP_next = 100 × Level^1.35 as configurable default, while creator can edit parameters;
- anti-farm before XP award: repeated/copy-paste/single-character/antiflood messages;
- click-able mentions in level/achievement output;
- approved 50-template libraries must be supplied verbatim from content package, not invented;
- custom achievement limit by tariff;
- integrate rewards into new wallet/ledger idempotency layer.

## 13. Игровой профиль и игры

### ❌ Основной объём отсутствует
- profile gender selection;
- first gender change free; later currency + cooldown (price/cooldown configurable if not explicitly fixed);
- 🍌/🍒 growth;
- 🌿 cannabis economy;
- 🍾 bottles;
- ⚔️ duels;
- 🥊 fights;
- daily bonus;
- game stats / rankings;
- game module toggles;
- group_game_settings;
- global creator coefficients.

## 14. RP и отношения

### 🟡 Есть рабочая основа
- RP menu reply to another user;
- RP actions/templates stored separately;
- cooldown in DB;
- self/bot basic protection;
- relationships module and marriage prototype exist.

### 🔧 / ❌ Нужно
- full canonical RP list from v2.4;
- clickable mentions;
- approved text libraries exactly, no invented replacements;
- simple RP 30s / romantic 1m / gift 1m / proposal 10m defaults;
- VIP custom RP + owner ability to disable individual custom RP;
- RP anti-farm;
- marriages / marriage_proposals as explicit models;
- proposal expiration;
- one active marriage atomically;
- close competing proposals after accepted marriage;
- canonical fixed endings.

## 15. Задания, подарки, рейтинги

### ❌ Отсутствует
- daily_tasks;
- task_progress;
- rewards;
- completed list;
- items/inventory/gifts;
- group and optional global rankings by required categories.

## 16. Оживление чата и automation

### ✅ Есть база
- background worker;
- per-group enable flag;
- random interval;
- active candidate window configurable;
- no same template immediately twice;
- history events;
- candidate selection gives preference to less recently picked users.

### 🔧 Нужно строго по v2.4
- canonical default interval 15–20m;
- candidate window exactly 60m by default;
- bot must not auto-post again if there was no human activity after previous bot auto-message;
- phrase cannot repeat for at least 50 triggers per group, not just immediately;
- category rotation;
- 0/1/2+ active participant rules;
- pair events with distinct users;
- exclude left/banned/deleted;
- approved 200 auto-message library;
- quiet hours;
- game reminder priority.

## 17. Игровые напоминания и случайные события

### ❌ Отсутствует
- per-group reminder enable + per-action toggles;
- five approved 50-template reminder libraries (250 total);
- bold fixed final line;
- random event engine;
- approved pool registry: 240 templates / 10 types;
- rare/legendary reward cooldown rules;
- atomic “кто первый” winner;
- random event ledger integration;
- group boosts / personal boosts / gifts / losses.

## 18. Реклама

### ❌ Отсутствует
- advertisements;
- seller listings: mandatory subscription / ad posts / both;
- buyer marketplace;
- exact cards and contact advertiser button;
- ad_orders and state history;
- seller approve/reject;
- scheduled automatic ad publication;
- allowed seller intervals 30m/1h/3h/12h/24h;
- rotation of multiple orders;
- mandatory subscriptions max 3/group;
- duration/subscriber-count orders;
- renewal confirmation;
- seller local buyer blacklist;
- ad_reviews / rating;
- ad_disputes;
- immutable agreed content + re-approval on material change;
- creator broadcast proposal → invoice 500 Telegram Stars → approved broadcast.

## 19. Поддержка

### ❌ Отсутствует
- technical issue;
- bug;
- feature proposal;
- game proposal;
- contact bot owner;
- user ticket history/status.

## 20. Панель создателя

### ❌ Отсутствует
- dashboard;
- groups/users;
- tariffs/payments;
- ads;
- support;
- broadcasts;
- games;
- diagnostics;
- system;
- editing prices/limits/cooldowns/economy/templates/global feature flags without code changes.

## 21. Системные ошибки / edge cases

### 🟡 Частично есть функциональные проверки
Но v2.4 утверждает конкретные pools/финальные части. Нужен единый ErrorContentProvider для:
- insufficient coins;
- cooldown;
- self-target;
- bot-target;
- user left;
- relationship errors;
- gift errors;
- fast-event errors.

Edge cases must use telegram_user_id, preserve data after leave, preserve marriage, mark deleted accounts, lock concurrent finances, idempotent reward transaction_id, atomic marriage acceptance and first-winner events.

# Приоритет разработки

## Phase 0 — заморозка контракта v2.4 и рефакторинг фундамента
1. Закрепить этот документ как roadmap в repo.
2. Создать owner/member/status/permission/audit/wallet/transaction-id foundation.
3. Не ломать текущие рабочие функции во время миграции.
4. Добавить services/repositories boundaries для новых модулей.

## Phase 1 — подключение групп + личный кабинет владельца + права
Это базовый UX v2.4, без него все следующие настройки будут временными командами.

## Phase 2 — полная ручная модерация + ранги + protections
Сохранить уже проверенные filter_sets и встроить их в новый UI/permission/audit framework.

## Phase 3 — пользовательская карточка + message stats + audience analytics

## Phase 4 — тарифы + subscriptions + addons + lifecycle timers

## Phase 5 — networks + network moderation

## Phase 6 — advertising marketplace + orders + mandatory subscriptions + Stars creator ads

## Phase 7 — game foundation: profile, wallet/inventory, cooldown service, anti-farm

## Phase 8 — games + tasks + gifts + rankings

## Phase 9 — RP/marriage full v2.4 + VIP custom RP

## Phase 10 — automation: reminders + 200 revive messages + 240 random events + quiet hours

## Phase 11 — support + creator panel + global configuration

## Phase 12 — final UX/catalog/audit/edge-case/load/regression pass

# Решение по уже существующему коду

Не переписывать проект с нуля. Текущий бот — полезный functional prototype. Сохраняем проверенные механики (activity, XP, transactions, RP engine, relationships, auto worker, filter sets) и постепенно переносим их под v2.4 architecture.

Последние изменения achievement_router/achievement_service, которые ещё не развёрнуты на VPS, не считаются production release. Они совместимы с направлением v2.4, но перед деплоем будут включены в новый wallet/audit/idempotency foundation или скорректированы.

# Правило деплоя на текущем этапе

Пока Phase 0 не завершён: VPS не обновлять. GitHub branch bootstrap/v0.1 используется как рабочая ветка архитектурной перестройки. После первого согласованного крупного milestone будет один controlled migration + rebuild + smoke test.
