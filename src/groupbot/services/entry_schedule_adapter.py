from __future__ import annotations

from types import ModuleType

from groupbot.services.protection_schedule import protection_enabled


def install_entry_schedule(module: ModuleType) -> None:
    """Make captcha and anti-raid respect the common protection schedule.

    The adapter only changes the effective enabled flag. All entry-protection
    settings and runtime logic stay in the existing entry_protection module.
    """
    base_captcha_cfg = module._captcha_cfg
    base_antiraid_cfg = module._antiraid_cfg

    def captcha_cfg(root):
        cfg = base_captcha_cfg(root)
        cfg["enabled"] = protection_enabled(root, "captcha", bool(cfg.get("enabled")))
        return cfg

    def antiraid_cfg(root):
        cfg = base_antiraid_cfg(root)
        cfg["enabled"] = protection_enabled(root, "antiraid", bool(cfg.get("enabled")))
        return cfg

    module._captcha_cfg = captcha_cfg
    module._antiraid_cfg = antiraid_cfg
