from __future__ import annotations

import inspect
from types import ModuleType

from groupbot.services.protection_schedule import protection_enabled


def _called_from_new_members() -> bool:
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back if frame and frame.f_back else None
        return bool(caller and caller.f_code.co_name == "new_members")
    finally:
        del frame


def install_entry_schedule(module: ModuleType) -> None:
    """Make captcha and anti-raid respect the common protection schedule.

    Only the runtime new-member handler receives the temporary schedule override.
    Settings screens and toggle callbacks continue to see the persistent values,
    so scheduled activation never changes or masks the group's normal settings.
    """
    base_captcha_cfg = module._captcha_cfg
    base_antiraid_cfg = module._antiraid_cfg

    def captcha_cfg(root):
        cfg = base_captcha_cfg(root)
        if _called_from_new_members():
            cfg["enabled"] = protection_enabled(root, "captcha", bool(cfg.get("enabled")))
        return cfg

    def antiraid_cfg(root):
        cfg = base_antiraid_cfg(root)
        if _called_from_new_members():
            cfg["enabled"] = protection_enabled(root, "antiraid", bool(cfg.get("enabled")))
        return cfg

    module._captcha_cfg = captcha_cfg
    module._antiraid_cfg = antiraid_cfg
