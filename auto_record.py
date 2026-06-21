"""Auto-recording: every action tool the user runs gets logged to a rolling
JSONL at ~/.webloom/auto_recording.jsonl, no opt-in required.

Why: the manual start_recording/end_recording flow is great but easy to forget.
Cracks have been lost because the author drove a real workflow + walked away
without ever arming the recorder. With auto-record, every tool call is on tape,
and the user (or their AI) can convert any time window into a Thread draft via
the webloom_capture_session tool.

Opt-out: set WEBLOOM_AUTO_RECORD=off.

The recording is INSIDE the engine, never leaves the user's machine. No
telemetry implications. Different from the per-tool playbook (which tracks
strategy-success counters per domain) — this is a chronological action log
ready to become a session_recipe.
"""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path
from typing import Any

AUTO_RECORD_ENABLED = os.environ.get("WEBLOOM_AUTO_RECORD", "on").lower() in ("on", "true", "1", "yes")
ROOT = Path.home() / ".webloom"
LOG_PATH = Path(os.environ.get("WEBLOOM_AUTO_RECORD_PATH", str(ROOT / "auto_recording.jsonl")))
DRAFTS_DIR = Path(os.environ.get("WEBLOOM_THREAD_DRAFTS_DIR", str(ROOT / "thread_drafts")))
MAX_LINES = int(os.environ.get("WEBLOOM_AUTO_RECORD_MAX", "10000"))

# Tools whose calls are worth logging (real DOM/network/navigation work).
# Mirrors recording.ACTION_TOOLS plus a few more.
ACTION_TOOLS = {
    "click", "fill", "navigate", "new_tab", "close_tab",
    "eval_js", "wait_for", "wait_for_idle", "scroll_tab",
    "key_type", "key_press",
    "upload_file", "xhr_upload",
    "lexical_set_text", "draftjs_set_text",
    "react_force_change", "redux_dispatch", "aui_dispatch",
    "replay_xhr", "inject_on_new_document", "remove_injected_script",
    "vision_check", "click_at_coords",
    "enable_stealth", "solve_captcha",
    "touch_tap",
    "reddit_submit_comment",
    "pause_for_human",
    "react_invoke_handler",
    "tiktok_post_video", "x_create_tweet",
}


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"https?://([^/?#]+)", url)
    if not m:
        return None
    host = m.group(1).lower()
    # Strip port + www
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _extract_domain(args: dict) -> str | None:
    """Best-effort: pull the target domain from the tool args.
    Falls back to None if the tool doesn't carry URL/domain context."""
    if not isinstance(args, dict):
        return None
    url = args.get("url")
    if url:
        d = _domain_from_url(str(url))
        if d:
            return d
    # eval_js with location.href / location.host hint in the code
    code = args.get("code", "") if args.get("code") else ""
    if code:
        m = re.search(r"https?://([^/'\"]+)", code)
        if m:
            d = _domain_from_url("https://" + m.group(1))
            if d:
                return d
    # tab parameter sometimes carries a URL substring
    tab = str(args.get("tab", ""))
    if tab and tab.startswith("http"):
        d = _domain_from_url(tab)
        if d:
            return d
    # selector arg with a domain — extremely rare, ignore
    return None


def log_action(tool: str, args: dict, result_summary: str = "ok", error: str | None = None) -> None:
    """Append one action to the rolling JSONL. Never raises."""
    if not AUTO_RECORD_ENABLED:
        return
    if tool not in ACTION_TOOLS:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": int(time.time()),
            "tool": tool,
            "args": _trim_args(args),
            "result": (result_summary or "")[:200],
            "error": (error or None) and str(error)[:200],
            "domain": _extract_domain(args),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        _rotate_if_too_big()
    except Exception:
        # Auto-record must NEVER break a user action
        pass


def _trim_args(args: Any) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for k, v in args.items():
        if v is None:
            continue
        # Drop noisy keys + cap long strings
        if isinstance(v, str):
            out[k] = v[:4000]
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:400] for x in v[:20]]
        elif isinstance(v, dict):
            out[k] = {str(kk)[:80]: str(vv)[:400] for kk, vv in list(v.items())[:20]}
        else:
            out[k] = v
    return out


def _rotate_if_too_big() -> None:
    try:
        if not LOG_PATH.exists():
            return
        with open(LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_LINES:
            return
        keep = lines[-MAX_LINES:]
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except Exception:
        pass


def _carry_navigate_domain(rows: list[dict]) -> list[dict]:
    """For actions that didn't carry an explicit URL/domain, attribute them to
    the most recent navigate's domain. Mirrors how an author actually thinks
    about a session: 'I navigated to X, then everything I did was on X.'"""
    current = None
    out = []
    for r in rows:
        d = r.get("domain")
        if r.get("tool") == "navigate" and d:
            current = d
        elif not d and current:
            r = {**r, "domain": current}
        out.append(r)
    return out


def capture_session(domain: str, last_minutes: int = 60) -> dict:
    """Slice the rolling log for `domain` within `last_minutes`, group into a
    candidate session_recipe, and write a draft Thread JSON to
    ~/.webloom/thread_drafts/<domain>.draft.json. Returns a summary."""
    if not LOG_PATH.exists():
        return {"ok": False, "error": "no auto-recording log yet — drive some actions first"}
    cutoff = int(time.time()) - max(60, last_minutes * 60)
    rows: list[dict] = []
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if int(r.get("ts", 0)) < cutoff:
                    continue
                rows.append(r)
    except Exception as e:
        return {"ok": False, "error": f"read log failed: {e}"}

    rows = _carry_navigate_domain(rows)
    target = domain.lower().strip()
    actions = [r for r in rows if (r.get("domain") or "").lower() == target]
    if not actions:
        return {
            "ok": False,
            "error": f"no actions for {target} in last {last_minutes} min",
            "hint": "Run the workflow on the target site in this engine session, then call capture again.",
        }

    # Build a session_recipe from the actions
    steps = []
    for a in actions:
        step: dict = {"kind": a["tool"]}
        args = a.get("args") or {}
        for k, v in args.items():
            if k in ("session",):
                continue
            step[k] = v
        if a.get("result") and a["result"] != "ok":
            step["_result_hint"] = a["result"]
        steps.append(step)

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft = {
        "domain": target,
        "name": f"{target} captured workflow",
        "version": "0.1.0-draft",
        "author": "WebLoom",
        "tier": "stub",
        "framework": "unknown",
        "notes": [
            "Auto-captured by webloom_capture_session from a real engine session.",
            f"{len(steps)} actions over {last_minutes} min window.",
            "Review + edit before publishing. Strip any secrets, parameterize variable inputs, add preflight checks.",
        ],
        "session_recipes": [
            {
                "id": "captured",
                "name": "Captured workflow",
                "outcome": "success",
                "params": {},
                "steps": steps,
            }
        ],
        "created_at": int(time.time()),
        "created_by": "webloom_capture_session",
    }
    draft_path = DRAFTS_DIR / f"{target}.draft.json"
    draft_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "domain": target,
        "action_count": len(steps),
        "window_minutes": last_minutes,
        "draft_path": str(draft_path),
        "hint": "Open the draft, edit/parameterize as needed, then move to ~/.webloom/threads/ and publish via webloom_publish_thread().",
    }


def status() -> dict:
    """Lightweight status for surfacing in webloom_engine_status / chrome_status."""
    if not AUTO_RECORD_ENABLED:
        return {"enabled": False}
    if not LOG_PATH.exists():
        return {"enabled": True, "logged_actions": 0}
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            n = sum(1 for _ in f)
    except Exception:
        n = -1
    return {"enabled": True, "logged_actions": n, "log_path": str(LOG_PATH)}
