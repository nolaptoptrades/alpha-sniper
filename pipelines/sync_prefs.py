"""
sync_prefs.py — NLT Alpha Sniper

Shared sync preference helpers.
Imported by cli.py (after EULA) and manager.py (fallback on direct launch).

Never blocks startup — all errors are caught silently.
"""

import json
import os


def _find_config_path() -> str:
    """Locate config.json — mirrors paths.py logic without the import chain."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"),
        os.path.expanduser("~/nolaptoptrades/config.json"),
    ]
    for p in candidates:
        p = os.path.normpath(p)
        if os.path.exists(p):
            return p
    raise FileNotFoundError("config.json not found")


def get_sharing_enabled() -> bool | None:
    """
    Return current sharing_enabled value from config.json.
    Returns None if the key has never been set.
    """
    try:
        with open(_find_config_path()) as f:
            cfg = json.load(f)
        data = cfg.get("data") or {}
        if "sharing_enabled" not in data:
            return None
        return bool(data["sharing_enabled"])
    except Exception:
        return None


def set_sharing_enabled(value: bool) -> bool:
    """Write sharing_enabled to config.json. Returns True on success."""
    try:
        path = _find_config_path()
        with open(path) as f:
            cfg = json.load(f)
        if "data" not in cfg:
            cfg["data"] = {}
        cfg["data"]["sharing_enabled"] = value
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def prompt_sync_preference() -> None:
    """
    Ask user once whether to enable anonymous trade data sync.
    Only fires if sharing_enabled is not yet set in config.
    Safe to call from both cli.py and manager.py.
    Never raises — all errors caught silently.
    """
    try:
        if get_sharing_enabled() is not None:
            return  # already set, never ask again

        print()
        print("  ─────────────────────────────────────────────")
        print("  Data Sync — optional")
        print()
        print("  Contribute anonymous trade simulation data to")
        print("  the NLT network. No API keys, no wallet")
        print("  addresses, no personal info — trade outcomes")
        print("  only.")
        print()
        print("  Change anytime: alphas --sync-on / --sync-off")
        print()

        try:
            answer = input("  Enable sync? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = "n"

        enabled = answer in ("", "y", "yes")
        set_sharing_enabled(enabled)

        if enabled:
            print("  ✓ Sync enabled — thank you for contributing.")
        else:
            print("  ✓ Sync disabled — enable later with: alphas --sync-on")
        print()

    except Exception:
        pass  # never block startup
