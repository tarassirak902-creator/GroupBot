import json
import random
from pathlib import Path

CONTENT_ROOT = Path(__file__).resolve().parent.parent / "content"


def _load_templates(filename: str) -> list[str]:
    path = CONTENT_ROOT / filename
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("templates") or []


def render_level_up(username: str, level: int) -> str:
    templates = _load_templates("level_up.json")
    if not templates:
        return f"{username}\n🎉 Достигнут {level} уровень!"
    template = random.choice(templates)
    return template.replace("{Username}", username).replace("{Level}", str(level))


def render_achievement(username: str, achievement_name: str) -> str:
    templates = _load_templates("achievement.json")
    if not templates:
        return f"{username}\n🏅 Получено достижение: {achievement_name}"
    template = random.choice(templates)
    return (
        template.replace("{Username}", username)
        .replace("{AchievementName}", achievement_name)
    )
