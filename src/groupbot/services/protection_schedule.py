from __future__ import annotations

from datetime import datetime, timedelta, timezone

MODULES = {
    "antiflood": "💬 Антифлуд",
    "antispam": "🔁 Антиспам",
    "antilinks": "🔗 Антиссылки",
    "words": "🚫 Запрещённые слова",
    "phrases": "📝 Запрещённые фразы",
    "captcha": "🧩 Капча",
    "antiraid": "🚨 Антирейд",
}


def schedule_config(root: dict | None) -> dict:
    raw = dict((root or {}).get("protection_schedule") or {})
    modules = [key for key in raw.get("modules", []) if key in MODULES]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "start": str(raw.get("start") or "23:00"),
        "end": str(raw.get("end") or "07:00"),
        "days": str(raw.get("days") or "daily"),
        "utc_offset": int(raw.get("utc_offset") or 0),
        "modules": modules,
    }


def _minutes(value: str) -> int | None:
    try:
        hour_s, minute_s = value.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _day_matches(local_dt: datetime, days: str, *, overnight_after_midnight: bool) -> bool:
    # For an overnight window 23:00-07:00, 02:00 belongs to the previous
    # schedule day, so weekend/weekday selection remains intuitive.
    check_dt = local_dt - timedelta(days=1) if overnight_after_midnight else local_dt
    weekday = check_dt.weekday()
    if days == "weekdays":
        return weekday < 5
    if days == "weekends":
        return weekday >= 5
    return True


def schedule_active(root: dict | None, now: datetime | None = None) -> bool:
    cfg = schedule_config(root)
    if not cfg["enabled"]:
        return False
    start = _minutes(cfg["start"])
    end = _minutes(cfg["end"])
    if start is None or end is None or start == end:
        return False
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(timezone(timedelta(hours=cfg["utc_offset"])))
    minute = local.hour * 60 + local.minute
    if start < end:
        inside = start <= minute < end
        after_midnight = False
    else:
        inside = minute >= start or minute < end
        after_midnight = minute < end
    return inside and _day_matches(local, cfg["days"], overnight_after_midnight=after_midnight)


def protection_enabled(root: dict | None, module: str, normal_enabled: bool) -> bool:
    if normal_enabled:
        return True
    cfg = schedule_config(root)
    return module in cfg["modules"] and schedule_active(root)
