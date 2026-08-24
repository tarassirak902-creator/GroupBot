from __future__ import annotations

STANDARD_REASONS: dict[str, list[dict[str, str | None]]] = {
    "warning": [
        {"text": "Флуд", "duration": None},
        {"text": "Спам", "duration": None},
        {"text": "Оскорбление", "duration": None},
        {"text": "Провокация", "duration": None},
        {"text": "Нарушение правил", "duration": None},
    ],
    "mute": [
        {"text": "Флуд", "duration": "15м"},
        {"text": "Спам", "duration": "30м"},
        {"text": "Оскорбление", "duration": "1ч"},
        {"text": "Провокация", "duration": "2ч"},
        {"text": "Неадекватное поведение", "duration": "1д"},
    ],
    "ban": [
        {"text": "Реклама / спам", "duration": None},
        {"text": "Мошенничество", "duration": None},
        {"text": "Запрещённый контент", "duration": None},
        {"text": "Рейд / вредительство", "duration": None},
        {"text": "Повторные нарушения", "duration": None},
    ],
}


def configured_reasons_with_defaults(config: dict, action: str) -> list[dict]:
    """Return built-in reasons first, followed by group-specific custom reasons."""
    defaults = [dict(item) for item in STANDARD_REASONS.get(action, [])]
    raw = dict(config.get("punishment_reasons") or {})
    custom = [dict(item) for item in (raw.get(action) or [])]

    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for item in defaults + custom:
        text = str(item.get("text") or "").strip()
        duration = str(item.get("duration") or "").strip()
        key = (text.casefold(), duration.casefold())
        if not text or key in seen:
            continue
        seen.add(key)
        result.append({"text": text, "duration": duration or None})
    return result
