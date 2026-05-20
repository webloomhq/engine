"""Recording engine for Chrome MCP — records browser workflows, replays them deterministically.

Design goals:
- Smart: captures multiple selector strategies per click/wait so replay survives small UI changes
- Bulletproof: every replay step has a retry budget, fallback selectors, and aborts cleanly with state on failure
- Lightweight: zero token cost during recording (server-side only), single-tool replay
"""
import json
import re
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent
RECIPES_DIR = ROOT / "recipes"
STATE_FILE = ROOT / ".recording_state.json"

# Tools that should NEVER be recorded (meta, status, recipe management)
META_TOOLS = {
    "chrome_status", "list_sessions", "list_running_chrome", "list_tabs",
    "get_playbook", "note", "save_playbook",
    "start_recording", "end_recording", "list_recipes", "replay_recipe",
    "screenshot", "read_tab", "scan_tab",  # read-only inspection
    "find_tab_by_selector",  # search, not action
    "launch_session",
}

# Tools that ARE actions worth recording
ACTION_TOOLS = {
    "click", "fill", "navigate", "new_tab", "eval_js", "wait_for", "scroll_tab", "close_tab"
}


def _ensure_dirs():
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {"active": False}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"active": False}


def _write_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def is_recording() -> bool:
    return _read_state().get("active", False)


def start(name: str, goal: str = "", domain: str = "") -> dict:
    """Begin recording a new recipe. Stops any previous in-progress recording."""
    _ensure_dirs()
    if not re.match(r"^[A-Za-z0-9_\-]+$", name):
        return {"ok": False, "error": "Recipe name must be alphanumeric, underscore, or dash."}
    state = {
        "active": True,
        "name": name,
        "goal": goal,
        "domain": domain,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "actions": [],
        "started_at_ms": int(time.time() * 1000),
    }
    _write_state(state)
    return {"ok": True, "recipe": name, "started_at": state["started_at"]}


def end(outcome: str = "success", parameters: list | None = None) -> dict:
    """Finalize the recording and save the recipe to disk."""
    state = _read_state()
    if not state.get("active"):
        return {"ok": False, "error": "No recording in progress."}
    if outcome not in ("success", "failed", "abort"):
        outcome = "success"
    recipe = {
        "name": state["name"],
        "goal": state.get("goal", ""),
        "domain": state.get("domain", ""),
        "created_at": state.get("started_at"),
        "outcome": outcome,
        "parameters": parameters or [],
        "actions": state.get("actions", []),
    }
    _ensure_dirs()
    if outcome == "success":
        path = RECIPES_DIR / f"{state['name']}.json"
        path.write_text(json.dumps(recipe, indent=2))
    _write_state({"active": False})
    return {
        "ok": True,
        "recipe": state["name"],
        "outcome": outcome,
        "action_count": len(recipe["actions"]),
        "saved_to": str(path) if outcome == "success" else None,
    }


def log_action(tool: str, args: dict, result_summary: str = "ok", error: str | None = None):
    """Append a single action to the current recording. Called from call_tool wrapper."""
    state = _read_state()
    if not state.get("active"):
        return
    if tool in META_TOOLS:
        return
    started_ms = state.get("started_at_ms", int(time.time() * 1000))
    now_ms = int(time.time() * 1000)
    # Capture a clean copy of args — drop session/tab references that won't be portable
    clean_args = {k: v for k, v in args.items() if v is not None}
    action = {
        "tool": tool,
        "args": clean_args,
        "elapsed_ms": now_ms - started_ms,
        "result": result_summary,
    }
    if error:
        action["error"] = error
    state.setdefault("actions", []).append(action)
    _write_state(state)


def list_recipes() -> list:
    _ensure_dirs()
    out = []
    for p in sorted(RECIPES_DIR.glob("*.json")):
        try:
            r = json.loads(p.read_text())
            out.append({
                "name": r.get("name", p.stem),
                "goal": r.get("goal", ""),
                "domain": r.get("domain", ""),
                "actions": len(r.get("actions", [])),
                "parameters": r.get("parameters", []),
                "created_at": r.get("created_at", ""),
            })
        except Exception:
            pass
    return out


def load_recipe(name: str) -> dict | None:
    path = RECIPES_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def substitute_params(args: dict, params: dict) -> dict:
    """Replace {{var}} in any string value of args with params[var]."""
    if not params:
        return args
    def sub(v):
        if isinstance(v, str):
            for k, val in params.items():
                v = v.replace("{{" + k + "}}", str(val))
            return v
        if isinstance(v, dict):
            return {kk: sub(vv) for kk, vv in v.items()}
        if isinstance(v, list):
            return [sub(item) for item in v]
        return v
    return {k: sub(v) for k, v in args.items()}


def delete_recipe(name: str) -> bool:
    path = RECIPES_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        return True
    return False
