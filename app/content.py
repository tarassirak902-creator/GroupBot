import json
import random
from pathlib import Path

CONTENT_ROOT = Path(__file__).resolve().parent.parent / "content"


def render_level_up(username: str, level: int) -> str:
    path = CONTENT_ROOT / "level_up.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    templates = payload.get("templates") or []
    if not templates:
        return f"{username}\n🎉 Достигнут {level} уровень!"
    template = random.choice(templates)
    return template.replace("{Username}", username).replace("{Level}", str(level))
