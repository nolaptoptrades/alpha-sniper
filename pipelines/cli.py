"""
cli.py — NLT Alpha Sniper
Entry point dispatcher. Owns all argument parsing and pre-launch sequence.

Pre-launch order:
    1. parse flags → dispatch special commands (exit after)
    2. EULA check  → show + accept on first run or version change
    3. license     → one-line status print
    4. launch TUI

Add new flags here — never in manager.py.
"""

import argparse
import json
import os
import sys
import time
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

from paths import get_config, get_path, log_error, save_config_value


# ============================================================
# CONSTANTS
# ============================================================

EULA_STATE_FILENAME = "eula_accepted.json"


# ============================================================
# EULA STATE
# ============================================================

def _eula_state_path() -> str:
    try:
        cfg = get_config()
        return os.path.join(cfg["paths"]["state"], EULA_STATE_FILENAME)
    except Exception:
        return os.path.expanduser(
            "~/nolaptoptrades/alpha_sniper/state/eula_accepted.json"
        )


def _load_eula_state() -> dict:
    try:
        with open(_eula_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


EULA_SYNC_KEY      = os.environ.get("NLT_SYNC_KEY", "")
EULA_SYNC_ENDPOINT = os.environ.get("NLT_SYNC_ENDPOINT", "https://nlt-trades.frankykho1.workers.dev/eula")


def _sync_eula(eula_version: str, app_version: str, accepted_ts: str) -> bool:
    """
    POST EULA acceptance record to D1 via Cloudflare Worker.
    Silent — never blocks launch. Returns True on success.
    """
    try:
        import hashlib, socket
        user_id = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]

        payload = json.dumps({
            "user_id":      user_id,
            "eula_version": eula_version,
            "app_version":  app_version,
            "tier":         "free",
            "accepted_ts":  accepted_ts,
        }).encode("utf-8")

        req = urllib.request.Request(
            EULA_SYNC_ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-nlt-key":    EULA_SYNC_KEY,
                "User-Agent":   "NLT-AlphaSniper/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception as e:
        log_error("cli", f"eula sync failed: {e}")
        return False


def _save_eula_state(eula_version: str, app_version: str):
    path = _eula_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp        = path + ".tmp"
    accepted_ts = datetime.now(timezone.utc).isoformat()
    data = {
        "accepted":     True,
        "ts":           accepted_ts,
        "eula_version": eula_version,
        "app_version":  app_version,
        "synced":       False,
    }
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

    # Sync to D1 — silent, never blocks
    ok = _sync_eula(eula_version, app_version, accepted_ts)
    if ok:
        data["synced"] = True
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


def _check_eula():
    """
    Show EULA and require acceptance if not yet accepted or version changed.
    Blocks until user accepts. Ctrl+C exits cleanly.
    """
    from eula import EULA_DISPLAY, EULA_VERSION, APP_VERSION, TERMS_URL, CONTACT

    state = _load_eula_state()

    # Already accepted this version — nothing to do
    if state.get("accepted") and state.get("eula_version") == EULA_VERSION:
        # Retry sync if previous attempt failed
        if not state.get("synced"):
            ok = _sync_eula(
                state["eula_version"],
                state.get("app_version", APP_VERSION),
                state.get("ts", datetime.now(timezone.utc).isoformat()),
            )
            if ok:
                state["synced"] = True
                path = _eula_state_path()
                with open(path, "w") as f:
                    json.dump(state, f, indent=2)
        return

    # Show EULA
    print(EULA_DISPLAY)

    if state.get("eula_version") and state.get("eula_version") != EULA_VERSION:
        print(f"  Terms updated (v{state['eula_version']} → v{EULA_VERSION}).")
        print(f"  Please review the changes above before continuing.\n")

    print("  By pressing Enter you confirm you have read and agree to these terms.")
    print("  Press Ctrl+C to exit.\n")

    try:
        input("  Press Enter to accept and continue... ")
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Exiting. You must accept the terms to use NLT Alpha Sniper.")
        sys.exit(0)

    _save_eula_state(EULA_VERSION, APP_VERSION)
    from sync_prefs import prompt_sync_preference
    prompt_sync_preference()
    print()



# ============================================================
# FLAG HANDLERS
# ============================================================

def _handle_version():
    from eula import APP_VERSION, EULA_VERSION
    print(f"NLT Alpha Sniper v{APP_VERSION} — Terms v{EULA_VERSION}")


def _handle_sync_on():
    from sync_prefs import set_sharing_enabled
    ok = set_sharing_enabled(True)
    if ok:
        print("✓ Sync enabled — anonymous trade data will be contributed to the NLT network.")
    else:
        print("✗ Failed to update config — check config.json is writable.")


def _handle_sync_off():
    from sync_prefs import set_sharing_enabled
    ok = set_sharing_enabled(False)
    if ok:
        print("✓ Sync disabled — no data will be sent.")
        print("  Re-enable anytime with: alphas --sync-on")
    else:
        print("✗ Failed to update config — check config.json is writable.")



def _handle_logs(component: str):
    """Tail a component log file directly from CLI."""
    import subprocess

    # Build name→log mapping from SCRIPTS
    try:
        from manager_config import SCRIPTS
        name_map = {s["name"].lower(): s["log"] for s in SCRIPTS}
        # Also accept key numbers
        key_map  = {s["key"]: s["log"] for s in SCRIPTS}
    except Exception:
        print("ERROR: could not load manager_config")
        sys.exit(1)

    log_path = name_map.get(component.lower()) or key_map.get(component)
    if not log_path:
        available = ", ".join(name_map.keys())
        print(f"Unknown component '{component}'. Available: {available}")
        sys.exit(1)

    if not os.path.exists(log_path):
        print(f"Log file not found: {log_path}")
        sys.exit(1)

    print(f"Tailing {log_path} — Ctrl+C to stop\n")
    try:
        subprocess.run(["tail", "-f", log_path])
    except KeyboardInterrupt:
        pass


def _handle_reset():
    """Clear state files — fresh start without losing logs or trade data."""
    try:
        cfg        = get_config()
        state_dir  = cfg["paths"]["state"]
    except Exception:
        print("ERROR: could not load config")
        sys.exit(1)

    state_files = [
        f for f in os.listdir(state_dir)
        if f.endswith(".json") or f.endswith(".txt")
    ]

    if not state_files:
        print("No state files found — already clean.")
        return

    print(f"State files to clear ({state_dir}):")
    for f in state_files:
        print(f"  {f}")
    print()

    try:
        confirm = input("Clear all state files? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return

    if confirm != "y":
        print("Cancelled.")
        return

    cleared = 0
    for f in state_files:
        try:
            os.remove(os.path.join(state_dir, f))
            cleared += 1
        except Exception as e:
            print(f"  could not remove {f}: {e}")

    print(f"Cleared {cleared} state file(s). Run 'nlt' to start fresh.")


def _export_report(lines: list, label: str) -> str:
    """Write report lines to a timestamped txt file. Returns path."""
    try:
        cfg       = get_config()
        base_dir  = cfg.get("base_dir", os.path.expanduser("~/nolaptoptrades"))
        reports_dir = os.path.join(base_dir, "alpha_sniper", "reports")
    except Exception:
        reports_dir = os.path.expanduser("~/nolaptoptrades/alpha_sniper/reports")

    os.makedirs(reports_dir, exist_ok=True)

    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    filename = f"{label}_{ts}.txt"
    path     = os.path.join(reports_dir, filename)

    # Strip ANSI/box chars for plain readability in txt — keep as-is for now
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return path

    
def _handle_mystats(force_insights: bool = False, skip_insights: bool = False, export: bool = False):
    """Display local paper trade summary from master_summary.jsonl — tier-gated detail."""
    # ── Insights config ───────────────────────────────────────
    try:
        ins_cfg     = get_config().get("insights", {})
        auto        = ins_cfg.get("auto", False)
        ins_enabled = ins_cfg.get("enabled", True)
    except Exception:
        ins_cfg     = {}
        auto        = False
        ins_enabled = True

    if skip_insights:
        should_show_insights = False
    elif force_insights:
        should_show_insights = True
    else:
        should_show_insights = ins_enabled and auto

    # ── Export config ───────────────────────────────────────
    try:
        rep_cfg     = get_config().get("reports", {})
        auto_export = rep_cfg.get("auto_export", False)
    except Exception:
        auto_export = False

    if not export:
        export = auto_export

    # ── Read trades from master_summary ──────────────────────
    try:
        path = get_path("master_summary")
    except Exception:
        print("ERROR: could not resolve master_summary path")
        sys.exit(1)

    if not os.path.exists(path):
        print("No trade data found — run the pipeline first.")
        return

    trades = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass

    valid = [t for t in trades if t.get("is_valid", True)]
    total = len(valid)
    if total == 0:
        print("No valid trades recorded yet.")
        return

    # ── Core stats ────────────────────────────────────────────
    wins     = [t for t in valid if not t.get("is_loss", False)]
    losses   = [t for t in valid if t.get("is_loss", False)]
    win_rate = round(len(wins) / total * 100, 1)

    rois      = [t["exit_roi_pct"] for t in valid if t.get("exit_roi_pct") is not None]
    avg_roi   = round(sum(rois) / len(rois), 2) if rois else 0.0
    win_rois  = [t["exit_roi_pct"] for t in wins if t.get("exit_roi_pct") is not None]
    loss_rois = [t["exit_roi_pct"] for t in losses if t.get("exit_roi_pct") is not None]
    avg_win   = round(sum(win_rois) / len(win_rois), 2) if win_rois else 0.0
    avg_loss  = round(sum(loss_rois) / len(loss_rois), 2) if loss_rois else 0.0

    win_rate_dec = len(wins) / total
    ev           = round((win_rate_dec * avg_win) + ((1 - win_rate_dec) * avg_loss), 2)

    dates  = [t["exit_date"] for t in valid if t.get("exit_date")]
    period = f"{min(dates)} → {max(dates)}" if dates else "unknown"

    # ── By reason ─────────────────────────────────────────────
    by_reason = {}
    for t in valid:
        r = t.get("exit_reason", "UNKNOWN")
        if r not in by_reason:
            by_reason[r] = {"count": 0, "rois": []}
        by_reason[r]["count"] += 1
        if t.get("exit_roi_pct") is not None:
            by_reason[r]["rois"].append(t["exit_roi_pct"])

    # ── Liq band breakdown ────────────────────────────────────
    def _liq_band(liq):
        if liq is None:           return None
        if liq < 100_000:         return "<100K"
        if liq < 200_000:         return "100K-200K"
        return "200K+"

    liq_bands = {}
    for t in valid:
        band = _liq_band(t.get("entry_liq_usd"))
        if band is None:
            continue
        if band not in liq_bands:
            liq_bands[band] = {"count": 0, "wins": 0, "rois": []}
        liq_bands[band]["count"] += 1
        if not t.get("is_loss", False):
            liq_bands[band]["wins"] += 1
        if t.get("exit_roi_pct") is not None:
            liq_bands[band]["rois"].append(t["exit_roi_pct"])

    liq_order = ["<100K", "100K-200K", "200K+"]

    # ── Extended breakdowns ───────────────────────────────────
    score_bands = mom_bands = bsr_bands = {}
    score_order = mom_order = bsr_order = []
    shadow_trades = []
    avg_peak = avg_exit_s = left_on_table = None

    if True:
        def _score_band(s):
            if s is None:    return None
            if s < 60:       return "<60"
            if s <= 75:      return "60-75"
            if s <= 90:      return "76-90"
            return "91-100"

        score_bands = {}
        for t in valid:
            band = _score_band(t.get("brain_score"))
            if band is None:
                continue
            if band not in score_bands:
                score_bands[band] = {"count": 0, "wins": 0, "rois": []}
            score_bands[band]["count"] += 1
            if not t.get("is_loss", False):
                score_bands[band]["wins"] += 1
            if t.get("exit_roi_pct") is not None:
                score_bands[band]["rois"].append(t["exit_roi_pct"])

        score_order = ["<60", "60-75", "76-90", "91-100"]

        def _mom_band(m):
            if m is None:   return None
            if m < 7:       return "3-7%"
            if m < 12:      return "7-12%"
            return "12-20%"

        mom_bands = {}
        for t in valid:
            band = _mom_band(t.get("entry_mom_pct"))
            if band is None:
                continue
            if band not in mom_bands:
                mom_bands[band] = {"count": 0, "wins": 0, "rois": []}
            mom_bands[band]["count"] += 1
            if not t.get("is_loss", False):
                mom_bands[band]["wins"] += 1
            if t.get("exit_roi_pct") is not None:
                mom_bands[band]["rois"].append(t["exit_roi_pct"])

        mom_order = ["3-7%", "7-12%", "12-20%"]

        def _bsr_band(b):
            if b is None:   return None
            if b < 1.3:     return "1.0-1.3"
            if b < 1.5:     return "1.3-1.5"
            if b < 2.0:     return "1.5-2.0"
            return "2.0+"

        bsr_bands = {}
        for t in valid:
            band = _bsr_band(t.get("entry_bsr"))
            if band is None:
                continue
            if band not in bsr_bands:
                bsr_bands[band] = {"count": 0, "wins": 0, "rois": []}
            bsr_bands[band]["count"] += 1
            if not t.get("is_loss", False):
                bsr_bands[band]["wins"] += 1
            if t.get("exit_roi_pct") is not None:
                bsr_bands[band]["rois"].append(t["exit_roi_pct"])

        bsr_order = ["1.0-1.3", "1.3-1.5", "1.5-2.0", "2.0+"]

        shadow_trades = [t for t in valid if t.get("peak_roi_pct") is not None]
        peak_rois     = [t["peak_roi_pct"] for t in shadow_trades]
        exit_rois_s   = [t["exit_roi_pct"] for t in shadow_trades if t.get("exit_roi_pct") is not None]
        avg_peak      = round(sum(peak_rois) / len(peak_rois), 2) if peak_rois else None
        avg_exit_s    = round(sum(exit_rois_s) / len(exit_rois_s), 2) if exit_rois_s else None
        left_on_table = round(avg_peak - avg_exit_s, 2) if avg_peak is not None and avg_exit_s is not None else None

    # ── Build output lines ───────────────────────────────────
    lines = []
    w = "══════════════════════════════════════════════"

    lines.append("")
    lines.append(w)
    lines.append(f"  NLT Alpha Sniper — My Trade Stats")
    lines.append(f"  Period: {period}")
    lines.append(w)
    lines.append(f"  Total trades     {total}  (valid only)")
    lines.append(f"  Win rate         {win_rate}%")
    lines.append(f"  Avg ROI          {avg_roi:+.2f}%")
    lines.append(f"  Avg win          {avg_win:+.2f}%")
    lines.append(f"  Avg loss         {avg_loss:+.2f}%")
    lines.append(f"  EV               {ev:+.2f}%")
    lines.append("")
    lines.append(f"  {'By reason:':<16}  {'trades':>6}   {'avg roi':>8}")
    lines.append(f"  {'─'*38}")
    for reason, d in sorted(by_reason.items(), key=lambda x: -x[1]["count"]):
        avg = round(sum(d["rois"]) / len(d["rois"]), 2) if d["rois"] else 0.0
        lines.append(f"  {reason:<16}  {d['count']:>6}   {avg:>+8.2f}%")

    lines.append("")
    lines.append(f"  {'By liq band:':<16}  {'trades':>6}   {'winrate':>7}   {'avg roi':>8}")
    lines.append(f"  {'─'*46}")
    for band in liq_order:
        d = liq_bands.get(band)
        if not d:
            continue
        wr  = round(d["wins"] / d["count"] * 100, 1)
        avg = round(sum(d["rois"]) / len(d["rois"]), 2) if d["rois"] else 0.0
        lines.append(f"  {band:<16}  {d['count']:>6}   {wr:>6.1f}%   {avg:>+8.2f}%")

    if True:
        lines.append("")
        lines.append(f"  {'By brain score:':<16}  {'trades':>6}   {'winrate':>7}   {'avg roi':>8}")
        lines.append(f"  {'─'*46}")
        for band in score_order:
            d = score_bands.get(band)
            if not d:
                continue
            wr  = round(d["wins"] / d["count"] * 100, 1)
            avg = round(sum(d["rois"]) / len(d["rois"]), 2) if d["rois"] else 0.0
            lines.append(f"  {band:<16}  {d['count']:>6}   {wr:>6.1f}%   {avg:>+8.2f}%")

        lines.append("")
        lines.append(f"  {'By momentum:':<16}  {'trades':>6}   {'winrate':>7}   {'avg roi':>8}")
        lines.append(f"  {'─'*46}")
        for band in mom_order:
            d = mom_bands.get(band)
            if not d:
                continue
            wr  = round(d["wins"] / d["count"] * 100, 1)
            avg = round(sum(d["rois"]) / len(d["rois"]), 2) if d["rois"] else 0.0
            lines.append(f"  {band:<16}  {d['count']:>6}   {wr:>6.1f}%   {avg:>+8.2f}%")

        lines.append("")
        lines.append(f"  {'By BSR band:':<16}  {'trades':>6}   {'winrate':>7}   {'avg roi':>8}")
        lines.append(f"  {'─'*46}")
        for band in bsr_order:
            d = bsr_bands.get(band)
            if not d:
                continue
            wr  = round(d["wins"] / d["count"] * 100, 1)
            avg = round(sum(d["rois"]) / len(d["rois"]), 2) if d["rois"] else 0.0
            lines.append(f"  {band:<16}  {d['count']:>6}   {wr:>6.1f}%   {avg:>+8.2f}%")

        if avg_peak is not None:
            lines.append("")
            lines.append(f"  Peak ROI insight: ({len(shadow_trades)}/{total} trades with shadow data)")
            lines.append(f"  {'─'*38}")
            lines.append(f"  Avg peak         {avg_peak:+.2f}%")
            lines.append(f"  Avg exit         {avg_exit_s:+.2f}%")
            lines.append(f"  Left on table    {left_on_table:+.2f}%")

    # ── Insights ──────────────────────────────────────────────
    if should_show_insights:
        insight_stats = {
            "total_trades": total,
            "win_rate_pct": win_rate,
            "avg_win_pct":  avg_win,
            "avg_loss_pct": avg_loss,
            "by_reason":    {r: d["count"] for r, d in by_reason.items()},
            "by_liq_band":  {b: {"count": liq_bands[b]["count"],
                                  "winrate_pct": round(liq_bands[b]["wins"] / liq_bands[b]["count"] * 100, 1),
                                  "avg_roi_pct": round(sum(liq_bands[b]["rois"]) / len(liq_bands[b]["rois"]), 2) if liq_bands[b]["rois"] else 0}
                              for b in liq_order if b in liq_bands},
        }
        insight_stats.update({
            "by_brain_score": {b: {"count": score_bands[b]["count"],
                                   "winrate_pct": round(score_bands[b]["wins"] / score_bands[b]["count"] * 100, 1),
                                   "avg_roi_pct": round(sum(score_bands[b]["rois"]) / len(score_bands[b]["rois"]), 2) if score_bands[b]["rois"] else 0}
                               for b in score_order if b in score_bands},
            "by_momentum":    {b: {"count": mom_bands[b]["count"],
                                   "winrate_pct": round(mom_bands[b]["wins"] / mom_bands[b]["count"] * 100, 1),
                                   "avg_roi_pct": round(sum(mom_bands[b]["rois"]) / len(mom_bands[b]["rois"]), 2) if mom_bands[b]["rois"] else 0}
                               for b in mom_order if b in mom_bands},
            "by_bsr_band":    {b: {"count": bsr_bands[b]["count"],
                                   "winrate_pct": round(bsr_bands[b]["wins"] / bsr_bands[b]["count"] * 100, 1),
                                   "avg_roi_pct": round(sum(bsr_bands[b]["rois"]) / len(bsr_bands[b]["rois"]), 2) if bsr_bands[b]["rois"] else 0}
                               for b in bsr_order if b in bsr_bands},
            "peak_roi_insight": {
                "sample":        len(shadow_trades),
                "avg_peak_pct":  avg_peak,
                "avg_exit_pct":  avg_exit_s,
                "left_on_table": left_on_table,
            } if avg_peak is not None else None,
        })

        print(f"  Generating insights...", end="\r")
        insight_text = _generate_insights(insight_stats)
        print(f"  {' ' * 30}", end="\r")

        lines.append("")
        lines.append(f"  {'─'*42}")
        lines.append(f"  Insights  ({ins_cfg.get('provider', 'gemini')})")
        lines.append(f"  {'─'*42}")

        bullets = re.split(r'\n(?=[⚠✓ℹ])', insight_text.strip())
        for bullet in bullets:
            clean = " ".join(bullet.split())
            if clean:
                words = clean.split()
                line  = ""
                for word in words:
                    if len(line) + len(word) + 1 > 60:
                        lines.append(f"  {line}")
                        line = "    " + word
                    else:
                        line = (line + " " + word).strip() if line else word
                if line:
                    lines.append(f"  {line}")

    lines.append(w)
    lines.append("")

    # ── Output: print or export ───────────────────────────────
    if export:
        path_out = _export_report(lines, "mystats")
        print(f"  Report saved: {path_out}")
    else:
        for line in lines:
            print(line)


def _generate_insights(stats: dict) -> str:
    try:
        cfg        = get_config()
        ins_cfg    = cfg.get("insights", {})
        provider   = ins_cfg.get("provider", "gemini").lower()
        models     = ins_cfg.get("model", {})
        max_tokens = int(ins_cfg.get("max_tokens", 1000))
        model_name = models.get(provider, "gemini-2.5-flash")
    except Exception as e:
        return f"ERROR: could not load insights config — {e}"

    prompt = f"""You are analyzing paper trade performance data for a Solana memecoin momentum sniper.

Stats data:
{json.dumps(stats, indent=2)}

Write exactly 4-5 insights. Strict rules:
- Each insight is ONE line, under 20 words
- Start each with: ⚠ (concern), ✓ (positive), or ℹ (observation)
- Reference EXACT numbers from the data — never round or approximate
- Compare specific bands against each other — never give generic advice
- FORBIDDEN: "tighten stop-loss", "enhance filters", "improve detection", "optimize strategy"
- Focus ONLY on: which specific bands outperform others and why, counterintuitive patterns, sample size caveats
- Format: [icon] [band/metric] at [exact number] vs [other band] at [exact number] — [specific implication]

Output only the bullet lines, nothing else."""

    MAX_RETRIES = 3
    TIMEOUT_SEC = 30

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if provider == "gemini":
                api_key = os.environ.get("GEMINI_API_KEY", "")
                if not api_key:
                    return "ERROR: GEMINI_API_KEY not set in .env"
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}",
                    data=json.dumps({
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": max_tokens}
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                    data = json.loads(resp.read().decode())
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

            elif provider == "claude":
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if not api_key:
                    return "ERROR: ANTHROPIC_API_KEY not set in .env"
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps({
                        "model":      model_name,
                        "max_tokens": max_tokens,
                        "messages":   [{"role": "user", "content": prompt}],
                    }).encode("utf-8"),
                    headers={
                        "Content-Type":      "application/json",
                        "x-api-key":         api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                    data = json.loads(resp.read().decode())
                    return data["content"][0]["text"].strip()

            else:
                return f"ERROR: unknown provider '{provider}' — use gemini or claude"

        except urllib.error.HTTPError as e:
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
                continue
            return f"ERROR: API returned {e.code} after {MAX_RETRIES} attempts"

        except Exception as e:
            if "timed out" in str(e).lower() and attempt < MAX_RETRIES:
                print(f"  Timeout — retrying ({attempt}/{MAX_RETRIES})...", end="\r")
                time.sleep(2 * attempt)
                continue
            return f"ERROR: {e}"

    return "ERROR: max retries exceeded"


def _handle_storage():
    """Show disk usage breakdown for cache and trade data."""
    try:
        logs_dir = get_path("logs")
    except Exception:
        print("ERROR: could not resolve logs path")
        sys.exit(1)

    # Cache files
    LOG_FILES = [
        "brain.log", "brain_w.log", "bridge_bot.log",
        "discovery.log", "safety.log", "simulator.log",
        "post_mortem.log", "sensor.log", "error.log",
    ]
    HANDSHAKE_FILES = [
        "safety.jsonl", "entry_verdicts.jsonl", "sniper_events.jsonl",
        "entry_queue.jsonl", "live_ticks.jsonl",
    ]
    # Trade data files
    TRADE_FILES     = ["paper_trades.jsonl", "paper_ticks.jsonl",
                       "shadow_ticks.jsonl", "wallet_ticks.jsonl"]
    # Protected files
    PROTECTED_FILES = ["master_summary.jsonl"]

    def _file_size(filename):
        path = os.path.join(logs_dir, filename)
        try:
            return os.path.getsize(path) if os.path.exists(path) else 0
        except Exception:
            return 0

    def _fmt(size):
        if size >= 1024 * 1024:
            return f"{size / (1024*1024):.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    w = "══════════════════════════════════════"
    print()
    print(w)
    print("  NLT Alpha Sniper — Storage")
    print(w)

    # Cache section
    print("  Cache  (--clear-cache)")
    print(f"  {'─'*36}")
    cache_total = 0
    for f in LOG_FILES + HANDSHAKE_FILES:
        size = _file_size(f)
        if size > 0:
            print(f"  {f:<30}  {_fmt(size):>8}")
            cache_total += size
    print(f"  {'─'*36}")
    print(f"  {'Total':<30}  {_fmt(cache_total):>8}")

    print()

    # Trade data section
    print("  Trade data  (--clear-trades)")
    print(f"  {'─'*36}")
    trade_total = 0
    for f in TRADE_FILES:
        size = _file_size(f)
        print(f"  {f:<30}  {_fmt(size):>8}")
        trade_total += size
    print(f"  {'─'*36}")
    print(f"  {'Total':<30}  {_fmt(trade_total):>8}")

    print()

    # Protected section
    print("  Protected  (never cleared)")
    print(f"  {'─'*36}")
    protected_total = 0
    for f in PROTECTED_FILES:
        size = _file_size(f)
        print(f"  {f:<30}  {_fmt(size):>8}")
        protected_total += size
    print(f"  {'─'*36}")
    print(f"  {'Total':<30}  {_fmt(protected_total):>8}")

    print()
    grand_total = cache_total + trade_total + protected_total
    print(f"  Grand total              {_fmt(grand_total):>8}")
    print(w)
    print()


def _handle_clear_cache():
    """Clear pipeline log and handshake files via clear_cache.py logic."""
    try:
        from clear_cache import _collect_targets
        targets = _collect_targets()
    except Exception as e:
        print(f"ERROR: could not load clear_cache — {e}")
        sys.exit(1)

    if not targets:
        print("Nothing to clear — all cache files already empty.")
        return

    def _fmt(size):
        if size >= 1024 * 1024:
            return f"{size / (1024*1024):.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    total_bytes = sum(s for _, _, s in targets)
    print()
    print("  The following cache files will be deleted:\n")
    for path, label, size in targets:
        print(f"  {label:<35}  {_fmt(size):>8}")
    print(f"\n  Total: {_fmt(total_bytes)}")
    print("  master_summary, paper_trades, shadow_ticks not touched.\n")

    try:
        confirm = input("  Delete all? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.")
        return

    if confirm != "y":
        print("  Cancelled.")
        return

    removed = 0
    for path, label, size in targets:
        try:
            os.remove(path)
            print(f"  ✓ removed {label}")
            removed += 1
        except Exception as e:
            print(f"  ✗ failed {label}: {e}")

    print(f"\n  Done — {removed}/{len(targets)} files cleared.")
    print()


def _handle_clear_trades():
    """Clear compiled trade records via clear_trades.py logic."""
    try:
        import sys as _sys
        import clear_trades
        old_argv = _sys.argv
        _sys.argv = [_sys.argv[0]]
        try:
            clear_trades.main()
        finally:
            _sys.argv = old_argv
    except SystemExit:
        pass
    except Exception as e:
        print(f"ERROR: could not run clear_trades — {e}")
        sys.exit(1)
        
# ============================================================
# MAIN
# ============================================================

def main():
    from eula import TERMS_SUMMARY

    parser = argparse.ArgumentParser(
        prog="nlt",
        description="NLT Alpha Sniper — Solana memecoin paper trading pipeline",
        epilog=TERMS_SUMMARY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--sync-on",
        action="store_true",
        help="Enable anonymous trade data sync",
    )
    parser.add_argument(
        "--sync-off",
        action="store_true",
        help="Disable anonymous trade data sync",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit",
    )
    parser.add_argument(
        "--logs",
        metavar="COMPONENT",
        help="Tail a component log (e.g. --logs discovery)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear state files for a fresh start",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Start with pipeline in dry-run mode (no writes)",
    )
    parser.add_argument(
        "--mystats",
        action="store_true",
        help="Show local paper trade summary",
    )
    parser.add_argument(
        "--insights",
        action="store_true",
        help="Generate AI insights with --mystats (overrides config)",
    )
    parser.add_argument(
        "--no-insights",
        action="store_true",
        help="Skip AI insights with --mystats (overrides config)",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Save report to a txt file instead of printing",
    )
    parser.add_argument(
        "--storage",
        action="store_true",
        help="Show disk usage for cache and trade data",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete pipeline log and handshake files",
    )
    parser.add_argument(
        "--clear-trades",
        action="store_true",
        help="Remove compiled trade records from raw files",
    )

    args = parser.parse_args()

    # ── Special commands — handle and exit ────────────────────
    if args.version:
        _handle_version()
        return

    if args.sync_on:
        _handle_sync_on()
        return

    if args.sync_off:
        _handle_sync_off()
        return

    if args.logs:
        _handle_logs(args.logs)
        return

    if args.reset:
        _handle_reset()
        return

    if args.mystats:
        _handle_mystats(
            force_insights=args.insights,
            skip_insights=args.no_insights,
            export=args.export,
        )
        return

    if args.storage:
        _handle_storage()
        return

    if args.clear_cache:
        _handle_clear_cache()
        return

    if args.clear_trades:
        _handle_clear_trades()
        return

    # ── Normal launch sequence ────────────────────────────────

    # 1. EULA — blocks only on first run or version change
    _check_eula()

    # 2. Launch TUI
    from manager import main as launch_manager
    launch_manager(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
