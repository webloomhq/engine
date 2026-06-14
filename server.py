#!/usr/bin/env python3
"""WebLoom — autonomous browser engine for AI agents.

The engine controls any real Chrome instance via CDP. Knowledge about specific
sites lives in installable "Threads" (JSON profile packs that ship separately),
and end-to-end automations live in "Weaves" (workflow bundles built on top of
Threads). WebLoom's core stays focused on browser primitives + the learning
mechanism; site-specific quirks are externalized so they can be authored,
shared, and sold without touching engine code.

  Engine  : WebLoom         (this file)
  Threads : profile packs   (~/.webloom/threads/<domain>.thread.json)
  Weaves  : workflow bundles (recipes + thread deps + parameter schema)
"""

import json
import os
import re
import base64
import asyncio
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
import websockets
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
import recording  # noqa: E402

_replaying = False  # suppresses log_action during replay_recipe

CHROME_EXE = os.environ.get(
    "CHROME_EXE",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)

def find_and_focus_chrome_window(port: int) -> int | None:
    """Find the specific Chrome window for this debug port, bring it to front."""
    import win32gui, win32process, win32con, win32con, ctypes, psutil

    # Step 1: find the PID(s) of the Chrome process using this debug port
    target_pids = set()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if 'chrome' in (proc.info['name'] or '').lower():
                cmdline = ' '.join(proc.info['cmdline'] or [])
                if f'--remote-debugging-port={port}' in cmdline:
                    target_pids.add(proc.info['pid'])
                    # Also include child processes
                    for child in proc.children(recursive=True):
                        target_pids.add(child.pid)
        except Exception:
            pass

    # Step 2: find the visible window owned by those PIDs
    best_hwnd = None
    best_area = 0

    def enum_handler(hwnd, _):
        nonlocal best_hwnd, best_area
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in target_pids:
            rect = win32gui.GetWindowRect(hwnd)
            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            if area > best_area and (rect[2]-rect[0]) > 400:
                best_area = area
                best_hwnd = hwnd

    win32gui.EnumWindows(enum_handler, None)
    if best_hwnd:
        try:
            win32gui.ShowWindow(best_hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        # Allow any process to set foreground, then focus via PowerShell
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass
        try:
            subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class W {{ [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int n); }}'; [W]::ShowWindow([IntPtr]{best_hwnd}, 9); [W]::SetForegroundWindow([IntPtr]{best_hwnd})"
            ], capture_output=True, timeout=3)
        except Exception:
            pass
        import time; time.sleep(0.5)
    return best_hwnd

async def get_exact_screen_coords(ws_url: str, text: str) -> tuple[float, float] | None:
    """Get exact physical screen coordinates — accounts for DPI scaling."""
    js = f"""(function() {{
        const all = document.querySelectorAll('*');
        const q = {json.dumps(text.lower())};
        const dpr = window.devicePixelRatio || 1;
        const toolbarCSS = window.outerHeight - window.innerHeight;
        for (const el of all) {{
            const t = (el.textContent || '').trim().toLowerCase();
            if (t.includes(q) && t.length < 200 && el.offsetParent !== null) {{
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {{
                    return JSON.stringify({{
                        x: Math.round((window.screenX + r.left + r.width/2) * dpr),
                        y: Math.round((window.screenY + toolbarCSS + r.top + r.height/2) * dpr)
                    }});
                }}
            }}
        }}
        return null;
    }})()"""
    result = await eval_in_tab(ws_url, js)
    val = result.get("result", {}).get("value")
    if val:
        try:
            coords = json.loads(val)
            return coords["x"], coords["y"]
        except Exception:
            pass
    return None

def real_cursor_click(screen_x: float, screen_y: float, hwnd=None):
    """Move the actual Windows cursor and click — undetectable to any website."""
    import pyautogui, win32gui, time
    pyautogui.PAUSE = 0.05

    # If window isn't foreground, click its title bar first to activate it
    if hwnd and win32gui.GetForegroundWindow() != hwnd:
        rect = win32gui.GetWindowRect(hwnd)
        title_x = (rect[0] + rect[2]) // 2
        title_y = rect[1] + 15  # title bar
        pyautogui.moveTo(title_x, title_y, duration=0.15)
        pyautogui.click()
        time.sleep(0.4)  # wait for window to activate

    pyautogui.moveTo(int(screen_x), int(screen_y), duration=0.2)
    time.sleep(0.1)
    pyautogui.click()

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool, TextContent, ImageContent,
    Prompt, PromptMessage, GetPromptResult,
)

SESSIONS_FILE = Path(os.environ.get(
    "CHROME_SESSIONS",
    str(Path(__file__).parent / "sessions.json")
))

_legacy_playbook = Path("D:/BrowserSessions/playbook.json")
_old_chromemcp_playbook = Path.home() / ".WebLoom" / "playbook.json"  # pre-rename
_default_playbook = Path.home() / ".webloom" / "playbook.json"
if _legacy_playbook.exists():
    _chosen_playbook = _legacy_playbook
elif _old_chromemcp_playbook.exists() and not _default_playbook.exists():
    _chosen_playbook = _old_chromemcp_playbook  # migration: keep reading old location until user moves it
else:
    _chosen_playbook = _default_playbook
PLAYBOOK_FILE = Path(os.environ.get("WEBLOOM_PLAYBOOK", os.environ.get("CHROME_PLAYBOOK", str(_chosen_playbook))))

# ── Telemetry + collective learning ────────────────────────────────────────
# Optional, opt-out-able telemetry that powers marketplace-level learning.
# Default ON; users can disable by setting WEBLOOM_TELEMETRY=off.
#
# Two streams:
#   1. Action telemetry — anonymous "strategy X worked on domain Y" rows.
#      Strict verification gate: only confidence >= 0.7 entries are sent.
#   2. Patch proposals — when drift_heal_suggest succeeds locally, propose
#      the heal back to the Thread author for AUTHOR-GATED review. Author
#      sees suggestion + corroboration count, manually clicks approve.
#
# Nothing is ever auto-applied. The marketplace data is ALWAYS just a
# suggestion; the human author is the final gate.
TELEMETRY_ENABLED = os.environ.get("WEBLOOM_TELEMETRY", "on").lower() in ("on", "true", "1", "yes")
TELEMETRY_URL = os.environ.get("WEBLOOM_TELEMETRY_URL", "https://webloom.run/api/telemetry")
PROPOSAL_URL = os.environ.get("WEBLOOM_PROPOSAL_URL", "https://webloom.run/api/patch-proposal")
ENGINE_VERSION = "0.2.0"
_ANON_ID_FILE = Path.home() / ".webloom" / "anon_id"


# Auto-update / auto-heal / auto-share — the community learning loop.
# Defaults: all three ON; users opt out with WEBLOOM_AUTO_UPDATE=off etc.
AUTO_UPDATE_ENABLED = os.environ.get("WEBLOOM_AUTO_UPDATE", "on").lower() in ("on", "true", "1", "yes")
AUTO_HEAL_ENABLED = os.environ.get("WEBLOOM_AUTO_HEAL", "on").lower() in ("on", "true", "1", "yes")
AUTO_SHARE_ENABLED = os.environ.get("WEBLOOM_AUTO_SHARE", "on").lower() in ("on", "true", "1", "yes")
AUTO_UPDATE_URL = os.environ.get("WEBLOOM_UPDATE_URL", "https://webloom.run/api/threads")
_AUTO_UPDATE_INTERVAL_S = 6 * 3600  # 6 hours
_THREADS_DIR_PATH = Path.home() / ".webloom" / "threads"


def _auto_update_threads_once() -> dict:
    """For every Thread file in ~/.webloom/threads/, poll the marketplace for a
    newer version. If found, atomically overwrite. Logs heals to a notification
    file the engine can surface to the user.

    Returns a summary: {checked, updated, errors}. Never raises.
    """
    if not AUTO_UPDATE_ENABLED:
        return {"checked": 0, "updated": 0, "errors": 0, "skipped": "auto-update off"}
    if not _THREADS_DIR_PATH.exists():
        return {"checked": 0, "updated": 0, "errors": 0, "skipped": "no threads dir"}
    import urllib.request as _req
    checked = updated = errors = 0
    notif_log_path = Path.home() / ".webloom" / "auto_updates.jsonl"
    updates_list = []
    for f in _THREADS_DIR_PATH.glob("*.thread.json"):
        if ".bak." in f.name:
            continue
        domain = f.name.replace(".thread.json", "")
        try:
            with open(f, encoding="utf-8") as fh:
                local = json.load(fh)
            local_version = local.get("version", "0.1.0")
            checked += 1
            try:
                req = _req.Request(
                    f"{AUTO_UPDATE_URL}/{domain}/latest?version={local_version}",
                    headers={"User-Agent": f"webloom-engine/{ENGINE_VERSION}"},
                )
                with _req.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
            except Exception:
                errors += 1
                continue
            if data.get("up_to_date"):
                continue
            new_thread = data.get("thread")
            if not new_thread:
                continue
            # Atomic write: tmp file → rename. Never leaves a half-written Thread on disk.
            tmp = f.with_suffix(".thread.json.tmp")
            with open(tmp, "w", encoding="utf-8") as out:
                json.dump(new_thread, out, indent=2)
            tmp.replace(f)
            updated += 1
            updates_list.append({
                "domain": domain,
                "from_version": local_version,
                "to_version": new_thread.get("version"),
                "patches": new_thread.get("patch_log", [])[-3:] if isinstance(new_thread.get("patch_log"), list) else [],
            })
        except Exception:
            errors += 1
    if updates_list:
        try:
            import time as _t
            with open(notif_log_path, "a", encoding="utf-8") as nl:
                for u in updates_list:
                    nl.write(json.dumps({"ts": int(_t.time()), **u}) + "\n")
        except Exception:
            pass
    return {"checked": checked, "updated": updated, "errors": errors, "updates": updates_list}


def _get_anon_id() -> str:
    """Persistent anonymous install id. Random uuid stored once on disk.
    Used only to rate-limit + trust-score telemetry. Never tied to user identity."""
    try:
        if _ANON_ID_FILE.exists():
            return _ANON_ID_FILE.read_text().strip()
        import uuid
        new_id = "anon-" + uuid.uuid4().hex[:24]
        _ANON_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ANON_ID_FILE.write_text(new_id)
        return new_id
    except Exception:
        return "anon-unknown"


def _send_telemetry_fire_forget(payload: dict):
    """POST a single telemetry row to webloom.run in a background thread.
    Errors are swallowed silently — telemetry should NEVER block or break a
    user's flow."""
    if not TELEMETRY_ENABLED:
        return
    import threading, urllib.request
    def _do():
        try:
            req = urllib.request.Request(
                TELEMETRY_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": f"webloom-engine/{ENGINE_VERSION}"},
            )
            urllib.request.urlopen(req, timeout=4).read()
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _propose_patch_fire_forget(payload: dict):
    """Propose a drift patch (selector heal) to the marketplace.
    Always author-gated — proposals queue, author reviews, author clicks
    approve. Nothing auto-applies."""
    if not TELEMETRY_ENABLED:
        return
    import threading, urllib.request
    def _do():
        try:
            req = urllib.request.Request(
                PROPOSAL_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": f"webloom-engine/{ENGINE_VERSION}"},
            )
            urllib.request.urlopen(req, timeout=4).read()
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _verify_confidence(verified: bool, ok: bool, side_effect: bool = False) -> float:
    """Map (verified, ok, side_effect) → confidence in [0.0, 1.0].

    The strict three-tier model that powers marketplace learning:
        1.0  = action completed AND verify probe passed (toast appeared, URL
               changed to expected, response body had real result, etc.)
        0.7  = action fired with a measurable side effect (DOM mutated,
               request returned 2xx with non-empty body, URL changed)
        0.4  = action fired but no observable effect (event dispatched,
               nothing visibly happened)
        0.0  = the tool threw / hard-failed

    Marketplace ranking only counts entries with confidence >= 0.7.
    Lower-confidence rows still upload (for debugging) but are excluded
    from any auto-suggestion logic."""
    if not ok:
        return 0.0
    if verified:
        return 1.0
    if side_effect:
        return 0.7
    return 0.4


def load_sessions() -> dict:
    with open(SESSIONS_FILE) as f:
        return json.load(f)

def cdp_url(port: int) -> str:
    return f"http://localhost:{port}"

def get_tabs(port: int) -> list:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return []

def find_tab(tabs: list, tab_ref: str) -> dict | None:
    """Find tab by id, title substring, or URL substring.

    Fallback: first **real** tab (filters extension workers, devtools, about:blank).
    Avoids accidentally targeting about:blank when the user's ref doesn't match.
    """
    tab_ref = str(tab_ref)
    if tab_ref:
        for t in tabs:
            if t.get("id") == tab_ref:
                return t
            if tab_ref.lower() in t.get("title", "").lower():
                return t
            if tab_ref.lower() in t.get("url", "").lower():
                return t
    rt = real_tabs(tabs)
    if rt:
        return rt[0]
    if tabs:
        return tabs[0]
    return None


class _CDPConn:
    """Long-lived CDP websocket connection with monotonic IDs and id-keyed response futures.

    Replaces the old open-close-per-call pattern that caused mid-session disconnects
    and id=1 collisions under concurrent calls.
    """
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.send_lock = asyncio.Lock()
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: dict[str, list[asyncio.Future]] = {}
        self._subscribers: dict[str, list] = {}
        self._reader: asyncio.Task | None = None

    def subscribe(self, method: str, cb):
        self._subscribers.setdefault(method, []).append(cb)

    def unsubscribe(self, method: str, cb):
        try:
            self._subscribers.get(method, []).remove(cb)
        except ValueError:
            pass

    async def _connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=50_000_000, ping_interval=20, ping_timeout=20)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                mid = data.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(data)
                else:
                    method = data.get("method")
                    if method:
                        if method in self._events:
                            waiters = self._events.pop(method, [])
                            for fut in waiters:
                                if not fut.done():
                                    fut.set_result(data)
                        for cb in list(self._subscribers.get(method, [])):
                            try:
                                cb(data)
                            except Exception:
                                pass
        except Exception:
            pass
        finally:
            self._fail_pending(ConnectionError("CDP websocket closed"))

    def _fail_pending(self, exc: Exception):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for waiters in self._events.values():
            for fut in waiters:
                if not fut.done():
                    fut.set_exception(exc)
        self._events.clear()

    def is_alive(self) -> bool:
        return self.ws is not None and not getattr(self.ws, "closed", True)

    async def send(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if not self.is_alive():
            await self._connect()
        async with self.send_lock:
            self._id += 1
            mid = self._id
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[mid] = fut
            await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            data = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)
        return data.get("result", {})

    async def wait_event(self, method: str, timeout: float = 30.0) -> dict:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._events.setdefault(method, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            try:
                self._events.get(method, []).remove(fut)
            except ValueError:
                pass

    async def close(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None


_cdp_pool: dict[str, _CDPConn] = {}
_pool_lock = asyncio.Lock()


async def _get_conn(ws_url: str) -> _CDPConn:
    async with _pool_lock:
        conn = _cdp_pool.get(ws_url)
        if conn is None or not conn.is_alive():
            if conn is not None:
                await conn.close()
            conn = _CDPConn(ws_url)
            await conn._connect()
            _cdp_pool[ws_url] = conn
        return conn


async def cdp_send(ws_url: str, method: str, params: dict = None, retries: int = 2) -> dict:
    last_err = None
    for attempt in range(retries + 1):
        try:
            conn = await _get_conn(ws_url)
            return await conn.send(method, params)
        except Exception as e:
            last_err = e
            async with _pool_lock:
                old = _cdp_pool.pop(ws_url, None)
                if old is not None:
                    await old.close()
            if attempt < retries:
                await asyncio.sleep(0.5)
    raise last_err


async def cdp_browser_send(port: int, method: str, params: dict = None) -> dict:
    """Send a CDP command to the browser target (not a tab) — needed for Target.* methods.

    Note on Playwright `connect_over_cdp` coexistence: CDP allows multiple clients
    on the same port via separate WS connections to /json/version. If Playwright
    fails to attach while WebLoom is running, it's almost always because
    Playwright is trying to take exclusive control of the browser target. Run
    Playwright FIRST (it claims the browser target), then WebLoom can still
    connect to individual page targets; the reverse order can lock Playwright out.
    """
    with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=5) as r:
        version = json.loads(r.read())
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Browser websocket not available — Chrome may need --remote-debugging-port")
    return await cdp_send(ws_url, method, params)


async def navigate_and_wait_load(ws_url: str, url: str, timeout: float = 20.0) -> str:
    """Navigate a tab and wait for the page load event instead of a fixed sleep."""
    conn = await _get_conn(ws_url)
    await conn.send("Page.enable")
    wait_task = asyncio.create_task(conn.wait_event("Page.loadEventFired", timeout=timeout))
    nav_result = await conn.send("Page.navigate", {"url": url})
    err = nav_result.get("errorText")
    if err:
        wait_task.cancel()
        return f"Navigation error: {err}"
    try:
        await wait_task
        return f"Navigated to {url} (load fired)"
    except asyncio.TimeoutError:
        return f"Navigated to {url} (load event timed out after {timeout}s — page may still be loading)"


async def verify_click_effect(ws_url: str, snapshot_before: str) -> bool:
    """Return True if the page changed since snapshot_before — i.e. the click took effect.

    Detects URL change, document state change, body size change, modal/popup appearance.
    Used to catch silent rejections from sites that gate on event.isTrusted.
    """
    js = """JSON.stringify({
        url: location.href,
        ready: document.readyState,
        title: document.title,
        bodyLen: (document.body && document.body.innerText || '').length,
        htmlLen: (document.body && document.body.innerHTML || '').length,
        kids: document.body ? document.body.children.length : 0,
        modal: !!document.querySelector('[role=dialog], .modal, .popup, [aria-modal=true]'),
        listbox: document.querySelectorAll('[role=listbox], [role=menu], [role=tree]').length,
        expanded: document.querySelectorAll('[aria-expanded="true"]').length,
        portals: document.querySelectorAll('[data-radix-popper-content-wrapper], [data-headlessui-portal], [data-state="open"]').length,
        focused: document.activeElement ? document.activeElement.tagName + '#' + (document.activeElement.id || '') : ''
    })"""
    try:
        r = await eval_in_tab(ws_url, js)
        snapshot_after = r.get("result", {}).get("value", "")
        return snapshot_after != snapshot_before
    except Exception:
        return True  # if we can't verify, don't escalate


# ── Network capture + diff-scan fingerprints state ─────────────────────────────
_network_buffers: dict[str, list] = {}          # ws_url -> [{request_id, url, method, type, status, mimeType, ...}]
_network_active: dict[str, dict] = {}           # ws_url -> {req_cb, res_cb}
_diff_fingerprints: dict[str, dict] = {}        # ws_url -> last scan dict


# ── Stealth init script: applied via Page.addScriptToEvaluateOnNewDocument ─────
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(p);
}
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

async def apply_stealth_to_tab(ws_url: str):
    """Patch navigator.webdriver, plugins, languages, permissions for every new doc."""
    try:
        await cdp_send(ws_url, "Page.enable")
        await cdp_send(ws_url, "Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    except Exception:
        pass


# ── Blocker detection: CAPTCHA, 2FA prompts, login walls ───────────────────────
ANTI_BOT_JS = r"""(function() {
    const title = (document.title || '').toLowerCase();
    const text = (document.body && document.body.innerText || '').toLowerCase();
    const html = (document.documentElement && document.documentElement.outerHTML || '').toLowerCase();
    const signals = [];

    // Cloudflare
    if (/just a moment|checking your browser|please wait|enable javascript and cookies/i.test(title + ' ' + text)
        || document.querySelector('iframe[src*="challenges.cloudflare.com"], div.cf-browser-verification, div#challenge-form')
        || window.__cf_chl_opt || window._cf_chl_opt) {
        signals.push({type: 'cloudflare_challenge', confidence: 'high'});
    }
    // DataDome
    if (document.querySelector('iframe[src*="geo.captcha-delivery.com"], iframe[src*="datadome"]')
        || window.dataDome) {
        signals.push({type: 'datadome', confidence: 'high'});
    }
    // PerimeterX
    if (document.querySelector('iframe[src*="captcha.px"], iframe[src*="perimeterx"]')
        || window._pxAppId || window.PX) {
        signals.push({type: 'perimeterX', confidence: 'high'});
    }
    // Arkose Labs (FunCaptcha)
    if (document.querySelector('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]')
        || window.arkose) {
        signals.push({type: 'arkose', confidence: 'high'});
    }
    // Akamai Bot Manager
    if (document.querySelector('script[src*="akam"], iframe[src*="akamai"]')
        || /bm_sz|_abck/.test(document.cookie)) {
        signals.push({type: 'akamai', confidence: 'medium'});
    }
    // Generic "Access Denied" pages
    if (/access denied|forbidden|403/.test(title) && text.length < 500) {
        signals.push({type: 'generic_block', confidence: 'medium'});
    }
    // Empty shell — page loaded but DOM is essentially empty (likely SSR stub or anti-bot redirect)
    const bodyLen = text.length;
    const interactiveCount = document.querySelectorAll('button, a[href], input, textarea, select, [role=button]').length;
    if (bodyLen < 100 && interactiveCount < 3 && document.readyState === 'complete') {
        signals.push({type: 'empty_shell', confidence: 'medium', bodyLen, interactiveCount});
    }

    const verdict = signals.length === 0 ? 'normal' : signals[0].type;
    return JSON.stringify({
        verdict,
        signals,
        page: {title: document.title, url: location.href, bodyLen, interactiveCount, ready: document.readyState}
    });
})()"""


FRAMEWORK_DETECT_JS = r"""(function() {
    const findFiberKey = (node) => {
        if (!node) return null;
        for (const k of Object.keys(node)) {
            if (k.startsWith('__reactFiber')) return 'react-17+';
            if (k.startsWith('__reactInternalInstance')) return 'react-16';
            if (k.startsWith('__reactProps')) return 'react-props-only';
        }
        return null;
    };
    const candidates = [
        document.querySelector('#__next'),
        document.querySelector('#root'),
        document.querySelector('#app'),
        document.querySelector('#__nuxt'),
        document.querySelector('[data-reactroot]'),
        document.body,
        document.documentElement,
    ];
    const allEls = document.querySelectorAll('div, main, section, article');
    for (let i = 0; i < Math.min(50, allEls.length); i++) candidates.push(allEls[i]);

    const frameworks = [];
    for (const c of candidates) {
        const r = findFiberKey(c);
        if (r) { frameworks.push(r); break; }
    }
    if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__) {
        const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
        if (hook.renderers && hook.renderers.size > 0) frameworks.push('react-devtools-renderer');
    }
    if (document.querySelector('#__next')) frameworks.push('nextjs-root');
    if (document.querySelector('#__nuxt')) frameworks.push('nuxt-root');
    if (window.__NEXT_DATA__) frameworks.push('nextjs-data');
    if (window.__NUXT__) frameworks.push('nuxt');
    if (window.__REDUX_DEVTOOLS_EXTENSION__) frameworks.push('redux-devtools-installed');
    if (window.store && typeof window.store.dispatch === 'function') frameworks.push('redux-global-store');
    if (window.A && (window.A.declarative || window.A.version)) frameworks.push('amazon-aui');
    if (window.Backbone) frameworks.push('backbone-' + (window.Backbone.VERSION || '?'));
    if (window.Vue) frameworks.push('vue');
    if (document.querySelector('[ng-version]')) {
        frameworks.push('angular-' + document.querySelector('[ng-version]').getAttribute('ng-version'));
    }
    if (document.querySelector('[data-radix-popper-content-wrapper], [data-state]')) frameworks.push('radix');
    if (document.querySelector('[data-headlessui-state], [data-headlessui-portal]')) frameworks.push('headlessui');
    if (Array.from(document.querySelectorAll('[class*="svelte-"]')).filter(el => /\bsvelte-[a-z0-9]{6,}/.test(el.className.toString())).length > 3) frameworks.push('svelte');
    if (window.P && window.P.when && window.P.modules) frameworks.push('amazon-p-module-loader');

    return JSON.stringify({
        frameworks,
        primary: frameworks[0] || 'vanilla',
        indicators: {
            has_password_input: !!document.querySelector('input[type=password]'),
            has_file_input: !!document.querySelector('input[type=file]'),
            has_label_wrapped_file: !!document.querySelector('label input[type=file][hidden], label input[type=file].sr-only'),
            has_drop_zone: !!document.querySelector('[ondrop], [data-dropzone], .dropzone'),
            iframe_count: document.querySelectorAll('iframe').length,
        },
        page: {title: document.title, url: location.href, ready: document.readyState}
    });
})()"""


DETECT_BLOCKER_JS = r"""(function() {
  const html = (document.body && document.body.innerText || '').slice(0, 5000).toLowerCase();
  const hasIframe = (sub) => !!document.querySelector(`iframe[src*="${sub}" i]`);
  const hasEl = (sel) => !!document.querySelector(sel);
  const out = { blockers: [], ready: document.readyState };

  // CAPTCHAs
  if (hasIframe('recaptcha') || hasEl('.g-recaptcha')) out.blockers.push({type:'captcha', kind:'reCAPTCHA'});
  if (hasIframe('hcaptcha') || hasEl('.h-captcha')) out.blockers.push({type:'captcha', kind:'hCaptcha'});
  if (hasIframe('challenges.cloudflare.com') || /checking your browser|verify you are human|cloudflare/i.test(html))
    out.blockers.push({type:'captcha', kind:'Cloudflare/Turnstile'});
  if (hasIframe('geo.captcha-delivery.com')) out.blockers.push({type:'captcha', kind:'DataDome'});
  if (hasIframe('arkoselabs')) out.blockers.push({type:'captcha', kind:'Arkose/FunCaptcha'});

  // 2FA
  const otpInput = document.querySelector('input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="2fa" i], input[name*="totp" i], input[id*="otp" i], input[id*="2fa" i]');
  if (otpInput) {
    let kind = 'totp';
    if (/sms|phone|text|mobile/i.test(html)) kind = 'sms';
    if (/email/i.test(html) && !/phone|sms/i.test(html.slice(0, 1500))) kind = 'email-code';
    if (/authenticator|google authenticator|authy/i.test(html)) kind = 'totp';
    out.blockers.push({type:'2fa', kind, selector: otpInput.id ? '#'+otpInput.id : (otpInput.name ? `input[name="${otpInput.name}"]` : 'input[autocomplete="one-time-code"]')});
  } else if (/check your phone|tap to approve|approve from your|push notification|duo push/i.test(html)) {
    out.blockers.push({type:'2fa', kind:'push'});
  } else if (/use your security key|insert your security key|yubikey|webauthn/i.test(html)) {
    out.blockers.push({type:'2fa', kind:'hardware-key'});
  }

  // Login wall
  const pw = document.querySelector('input[type=password]');
  if (pw && pw.offsetParent !== null) out.blockers.push({type:'login-wall'});

  // Generic verification
  if (out.blockers.length === 0 && /verify|are you human|robot|challenge/i.test(html) && html.length < 2000) {
    out.blockers.push({type:'verification', kind:'generic'});
  }

  return JSON.stringify(out);
})()"""


# ── Vision Layer 2.5: ground a natural-language target to (x, y) in the viewport ──
# Backend chosen via env VISION_BACKEND: "claude" (default, needs ANTHROPIC_API_KEY) or "florence" (local GPU).
#
# Florence-2 is wired but NOT installed. Only switch to it if you build something
# that fires Layer 2.5 at high volume (auto-click farming, monitoring loops, signup
# scale) where Claude vision's ~$0.01/call becomes real money. For one-off / human-
# paced automation, Claude vision is the better default — higher accuracy, zero setup.
#
# To activate Florence later:
#   1. Install Python 3.12 to D:\Python312 (torch has no CUDA wheels for 3.14 yet)
#   2. python -m venv D:\WebLoom-vision-env
#   3. D:\WebLoom-vision-env\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cu121
#   4. ...\pip install transformers einops timm pillow fastapi uvicorn
#   5. Build a persistent localhost vision server in that venv (keeps Florence in VRAM
#      so WebLoom doesn't pay python-startup tax per click). WebLoom posts
#      screenshots via HTTP and gets (x, y) back. ~300ms/call, $0.
#   6. Set HF_HOME=D:\hf-cache so model weights (~1.5GB) land on D, not C.
#   7. Rewrite _vision_florence below to POST to the local server instead of importing torch.
#   8. Set VISION_BACKEND=florence.
VISION_BACKEND = os.environ.get("VISION_BACKEND", "claude").lower()

_florence_model = None
_florence_processor = None
_florence_device = None


def _load_florence():
    """Lazy-load Florence-2 to GPU on first use. Costs ~1.5GB VRAM."""
    global _florence_model, _florence_processor, _florence_device
    if _florence_model is not None:
        return
    import torch
    from transformers import AutoProcessor, AutoModelForCausalLM
    _florence_device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if _florence_device == "cuda" else torch.float32
    model_id = os.environ.get("FLORENCE_MODEL", "microsoft/Florence-2-base")
    _florence_processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    _florence_model = AutoModelForCausalLM.from_pretrained(model_id, trust_remote_code=True, torch_dtype=dtype).to(_florence_device)
    _florence_model.eval()


async def _vision_florence(image_b64: str, description: str) -> tuple[float, float] | None:
    """Run Florence-2 OPEN_VOCABULARY_DETECTION → return viewport center of best box."""
    import io, base64
    import torch
    from PIL import Image

    def _run():
        _load_florence()
        img = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")
        prompt = "<OPEN_VOCABULARY_DETECTION>" + description
        inputs = _florence_processor(text=prompt, images=img, return_tensors="pt").to(_florence_device)
        with torch.no_grad():
            gen = _florence_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"].to(_florence_model.dtype),
                max_new_tokens=256,
                num_beams=3,
                do_sample=False,
            )
        text = _florence_processor.batch_decode(gen, skip_special_tokens=False)[0]
        parsed = _florence_processor.post_process_generation(text, task="<OPEN_VOCABULARY_DETECTION>", image_size=(img.width, img.height))
        boxes = parsed.get("<OPEN_VOCABULARY_DETECTION>", {}).get("bboxes", [])
        if not boxes:
            return None
        x1, y1, x2, y2 = boxes[0]
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    return await asyncio.to_thread(_run)


async def _vision_claude(image_b64: str, description: str, viewport_w: int, viewport_h: int) -> tuple[float, float] | None:
    """Ask Claude vision for the (x, y) pixel center of the described element."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": os.environ.get("VISION_CLAUDE_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": (
                    f"This is a {viewport_w}x{viewport_h} browser viewport screenshot. "
                    f"Find the element best described as: \"{description}\".\n"
                    f"Return ONLY a JSON object with the pixel center of that element relative to the image, "
                    f"like {{\"x\": 123, \"y\": 456}}. "
                    f"If no element matches, return {{\"x\": null, \"y\": null}}. "
                    f"No prose, no markdown, just JSON."
                )},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    def _post():
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    try:
        data = await asyncio.to_thread(_post)
    except Exception:
        return None
    blocks = data.get("content", [])
    text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        obj = json.loads(text)
        x, y = obj.get("x"), obj.get("y")
        if x is None or y is None:
            return None
        return float(x), float(y)
    except Exception:
        return None


async def vision_ground(ws_url: str, description: str) -> tuple[float, float] | None:
    """Screenshot the tab → ask the vision backend for (x, y) → return viewport coords.
    Returns None if no model is configured or the element isn't found.

    Scrolls any element loosely matching the description into view BEFORE the
    screenshot — fixes the case where the button exists but is offscreen
    (e.g. KDP page pushed buttons down due to a banner).
    """
    try:
        scroll_js = f"""(function() {{
            const desc = {json.dumps(description.lower())};
            const all = document.querySelectorAll('button, a, input, [role=button], div, span, label');
            for (const el of all) {{
                const t = (el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').toLowerCase();
                if (t.includes(desc)) {{
                    el.scrollIntoView({{block:'center', inline:'center', behavior:'instant'}});
                    return true;
                }}
            }}
            return false;
        }})()"""
        await eval_in_tab(ws_url, scroll_js)
        await asyncio.sleep(0.15)  # let layout settle
    except Exception:
        pass
    image_b64 = await screenshot_tab(ws_url)
    if not image_b64:
        return None
    # Get viewport size for prompts that need it
    try:
        r = await eval_in_tab(ws_url, "JSON.stringify({w:innerWidth,h:innerHeight})")
        vp = json.loads(r.get("result", {}).get("value", "{}"))
        vw, vh = int(vp.get("w", 1280)), int(vp.get("h", 800))
    except Exception:
        vw, vh = 1280, 800

    if VISION_BACKEND == "florence":
        return await _vision_florence(image_b64, description)
    return await _vision_claude(image_b64, description, vw, vh)


# ── Actionability + playbook-informed click ────────────────────────────────────
ACTIONABILITY_JS = """(function(description) {
    // Locate by text/aria/placeholder OR CSS selector — same matchers as CLICK_JS.
    const desc = description.toLowerCase();
    const candidates = document.querySelectorAll(
        'button, a, input[type=submit], input[type=button], '
        + '[role=button], [role=link], [role=combobox], [role=option], '
        + '[role=menuitem], [role=tab], [aria-haspopup], [onclick], label, '
        + 'div, span, li'
    );
    let el = null;
    for (const c of candidates) {
        const t = (c.textContent || c.value || c.getAttribute('aria-label') || c.getAttribute('placeholder') || '').toLowerCase();
        if (t.includes(desc)) { el = c; break; }
    }
    if (!el) {
        try { el = document.querySelector(description); } catch (e) {}
    }
    if (!el) return JSON.stringify({found: false});

    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const style = getComputedStyle(el);

    const visible = (el.checkVisibility
        ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
        : (r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && parseFloat(style.opacity || '1') > 0));

    const inViewport = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    const notDisabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const hasArea = r.width >= 1 && r.height >= 1;

    let hitsTarget = false;
    let intercepting = null;
    try {
        const top = document.elementFromPoint(cx, cy);
        // Click at (cx, cy) will dispatch on `top` and bubble UP. So we accept:
        //   - top === el  (clicking el directly)
        //   - el.contains(top)  (clicking a child → event bubbles up to el)
        // We REJECT top.contains(el) — that means el is BEHIND `top` and the click
        // will land on the cover element, not reach el. Common when an invisible
        // overlay / wrapper steals hit-test from the actual button (Amazon AUI,
        // KDP modal alerts, etc.).
        hitsTarget = !!top && (top === el || el.contains(top));
        if (top && !hitsTarget) {
            intercepting = {
                tag: top.tagName,
                id: top.id || '',
                cls: (top.className || '').toString().slice(0, 100),
            };
        }
    } catch (e) {}

    let pendingAnim = 0;
    try { pendingAnim = (el.getAnimations ? el.getAnimations({subtree: true}).filter(a => a.playState === 'running').length : 0); } catch(e) {}

    return JSON.stringify({
        found: true,
        cx, cy,
        rect: {x: r.left, y: r.top, w: r.width, h: r.height},
        visible, inViewport, notDisabled, hasArea, hitsTarget,
        intercepting,  // populated when hit-test landed on a non-descendant of el
        animating: pendingAnim,
        actionable: visible && inViewport && notDisabled && hasArea && hitsTarget && pendingAnim === 0,
        tag: el.tagName, id: el.id || '', cls: (el.className || '').toString().slice(0, 80)
    });
})(DESCRIPTION)"""


async def check_actionability(ws_url: str, desc: str) -> dict:
    """One-shot actionability probe. Returns a dict (or {} on JS error)."""
    js = ACTIONABILITY_JS.replace("DESCRIPTION", json.dumps(desc))
    r = await eval_in_tab(ws_url, js)
    try:
        return json.loads(r.get("result", {}).get("value", "{}"))
    except Exception:
        return {}


async def wait_actionable(ws_url: str, desc: str, timeout_s: float = 5.0, interval_s: float = 0.12) -> dict:
    """Poll actionability until True, returning the final probe dict.
    Bails early as soon as actionable, or after timeout. Also confirms position
    stability by requiring two consecutive probes to agree within 1px.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_rect = None
    last = {}
    while asyncio.get_event_loop().time() < deadline:
        probe = await check_actionability(ws_url, desc)
        last = probe
        if not probe.get("found"):
            await asyncio.sleep(interval_s)
            continue
        if probe.get("actionable"):
            # Stability: require position unchanged across two probes
            r = probe.get("rect", {})
            if last_rect and abs(r.get("x", 0) - last_rect.get("x", 0)) < 1 and abs(r.get("y", 0) - last_rect.get("y", 0)) < 1:
                return probe
            last_rect = r
        await asyncio.sleep(interval_s)
    return last


# ── Playbook-informed strategy selection ───────────────────────────────────────
def _playbook_get_strategy(domain: str, desc: str) -> dict | None:
    """Returns {strategy, success_rate, last_at} if a usable history exists, else None."""
    if not domain:
        return None
    pb = load_playbook().get(domain, {})
    log = pb.get("click_log", {})
    entry = log.get(desc)
    if not entry:
        return None
    # Backward compat with old {strategy, worked} shape
    if "successes" not in entry and "failures" not in entry:
        return {
            "strategy": entry.get("strategy"),
            "success_rate": 1.0 if entry.get("worked") else 0.0,
            "successes": 1 if entry.get("worked") else 0,
            "failures": 0 if entry.get("worked") else 1,
            "last_at": None,
        }
    s, f = int(entry.get("successes", 0)), int(entry.get("failures", 0))
    total = s + f
    return {
        "strategy": entry.get("strategy"),
        "success_rate": (s / total) if total else 0.0,
        "successes": s, "failures": f,
        "last_at": entry.get("last_at"),
    }


# Per-domain in-memory last-action tracker for sequence + wait recording.
# Reset on tab navigation or after >10min idle.
_session_state: dict[str, dict] = {}  # domain -> {last_desc, last_kind, last_ts, last_selector}

def _playbook_record(
    domain: str,
    desc: str,
    strategy: str,
    success: bool,
    kind: str = "click",
    selector_pattern: str | None = None,
    manual_touch_required: bool = False,
    manual_touch_reason: str | None = None,
    confidence: float | None = None,
    verified: bool = False,
    verify_kind: str | None = None,
    ms: int | None = None,
):
    """Record (domain, desc, kind, strategy) success/failure with enriched context.

    Writes to two log shapes:
      - click_log (legacy, backward-compat) — kept so existing weaver promote works
      - action_log (enriched, v2) — captures wait timings, sequence (follows), selector pattern,
        manual-touch markers. Used by future weaver promote for full-flow Threads.

    Also (if telemetry is on) fires an anonymous row to webloom.run/api/telemetry
    so the marketplace can rank strategies. Confidence rules:
      - 1.0 = verified end-to-end (verify probe passed)
      - 0.7 = action fired with measurable side effect
      - 0.4 = action fired, no observable effect (default for legacy callers)
      - 0.0 = the tool errored
    Only confidence >= 0.7 entries influence marketplace ranking.
    """
    if not domain:
        return
    import time
    now = time.time()
    if confidence is None:
        # Legacy callers didn't pass confidence — assume a side effect happened
        # if success was True. They get the 0.7 tier (counts) by default.
        confidence = 0.7 if success else 0.0
    pb = _load_live_playbook_raw()
    if domain not in pb:
        pb[domain] = {}

    # ── legacy click_log shape ─────────────────────────────────────────────
    log = pb[domain].setdefault("click_log", {})
    entry = log.get(desc, {})
    if "successes" not in entry and "failures" not in entry:
        entry = {"strategy": strategy, "successes": 0, "failures": 0}
    if entry.get("strategy") != strategy:
        entry = {"strategy": strategy, "successes": 0, "failures": 0}
    if success:
        entry["successes"] = entry.get("successes", 0) + 1
    else:
        entry["failures"] = entry.get("failures", 0) + 1
    entry["last_at"] = int(now)
    log[desc] = entry

    # ── enriched action_log (v2) ────────────────────────────────────────────
    alog = pb[domain].setdefault("action_log", {})
    a = alog.get(desc, {})
    if not a:
        a = {
            "kind": kind,
            "strategy": strategy,
            "successes": 0,
            "failures": 0,
            "first_seen_at": int(now),
            "follows": [],            # observed predecessors (descriptors)
            "wait_before_samples": [],  # seconds since previous action
        }
    # If kind or strategy changed, keep counters but update current
    a["kind"] = kind
    a["strategy"] = strategy
    if success:
        a["successes"] = a.get("successes", 0) + 1
    else:
        a["failures"] = a.get("failures", 0) + 1
    a["last_at"] = int(now)
    if selector_pattern:
        a["selector_pattern"] = selector_pattern
    if manual_touch_required:
        a["manual_touch_required"] = True
        if manual_touch_reason:
            a["manual_touch_reason"] = manual_touch_reason

    # Sequence + wait_before: compare with last action on this domain.
    sess = _session_state.get(domain)
    if sess and sess.get("last_desc") and sess["last_desc"] != desc:
        gap = now - float(sess.get("last_ts", now))
        # Reset session if gap > 600s (10 min) — that's a new flow
        if gap < 600 and success:
            prev = sess["last_desc"]
            if prev not in a["follows"]:
                a["follows"].append(prev)
            samples = a.get("wait_before_samples", [])
            samples.append(round(gap, 2))
            a["wait_before_samples"] = samples[-10:]  # keep last 10
            # Also fill the previous action's wait_after via reverse pointer
            prev_entry = alog.get(prev)
            if prev_entry is not None:
                w_after = prev_entry.get("wait_after_samples", [])
                w_after.append(round(gap, 2))
                prev_entry["wait_after_samples"] = w_after[-10:]

    alog[desc] = a

    # Update session state
    if success:
        _session_state[domain] = {
            "last_desc": desc,
            "last_kind": kind,
            "last_ts": now,
            "last_selector": selector_pattern,
        }

    save_playbook_data(pb)

    # Marketplace telemetry — anonymous, opt-out-able, never blocks the flow
    try:
        _send_telemetry_fire_forget({
            "anon_id": _get_anon_id(),
            "domain": domain,
            "action_descriptor": desc,
            "strategy": strategy,
            "confidence": float(confidence),
            "ok": bool(success),
            "verified": bool(verified),
            "verify_kind": verify_kind,
            "ms": ms,
            "engine_version": ENGINE_VERSION,
        })
    except Exception:
        pass


def record_coverage_gap(
    domain: str,
    desc: str,
    reason: str,
    classification: str = "unknown",
    status: str = "open",
):
    """Record a step where automation hit a wall — a BUG TO CRACK, not a feature.

    Threads aim for zero coverage gaps. Each one logged here is a public TODO that
    either needs an engine fix (Strategy E for AjaxInput, AUI dispatch refinement,
    redux_dispatch tuning) or a Thread-specific strategy (a custom recipe step that
    the buyer's agent runs after a workaround).

    classification: 'engine_fix_needed' | 'thread_strategy_needed' | 'site_hostile' | 'unknown'
    status:         'open' | 'in_progress' | 'fixed_v1.0.X' | 'workaround_documented'
    """
    if not domain or not desc:
        return
    import time
    pb = _load_live_playbook_raw()
    if domain not in pb:
        pb[domain] = {}
    gaps = pb[domain].setdefault("coverage_gaps", [])
    existing = next((g for g in gaps if g.get("desc") == desc and g.get("status") == status), None)
    if existing:
        existing["last_seen_at"] = int(time.time())
        existing["hit_count"] = existing.get("hit_count", 1) + 1
        if reason and reason not in (existing.get("reasons") or []):
            existing.setdefault("reasons", []).append(reason)
    else:
        gaps.append({
            "desc": desc,
            "reason": reason,
            "classification": classification,
            "status": status,
            "first_seen_at": int(time.time()),
            "last_seen_at": int(time.time()),
            "hit_count": 1,
        })
    save_playbook_data(pb)
    return


def mark_manual_touch(domain: str, desc: str, reason: str):
    """Deprecated alias for record_coverage_gap. Use record_coverage_gap directly."""
    record_coverage_gap(domain, desc, reason, classification="unknown", status="open")
    return  # legacy placeholder so following code is unreachable safely


def _legacy_mark_manual_touch_unused(domain: str, desc: str, reason: str):
    """Old manual-touch impl, kept temporarily for diff-context safety."""
    if not domain or not desc:
        return
    import time
    pb = _load_live_playbook_raw()
    if domain not in pb:
        pb[domain] = {}
    touches = pb[domain].setdefault("manual_touches", [])
    touches.append({
        "desc": desc,
        "reason": reason,
        "at": int(time.time()),
    })
    # Also flag in action_log if present
    alog = pb[domain].setdefault("action_log", {})
    if desc in alog:
        alog[desc]["manual_touch_required"] = True
        alog[desc]["manual_touch_reason"] = reason
    save_playbook_data(pb)


def reset_session_state(domain: str | None = None):
    """Reset in-memory sequence tracker. Call on tab navigation / explicit boundary."""
    if domain:
        _session_state.pop(domain, None)
    else:
        _session_state.clear()


async def snapshot_for_verify(ws_url: str) -> str:
    js = """JSON.stringify({
        url: location.href,
        ready: document.readyState,
        title: document.title,
        bodyLen: (document.body && document.body.innerText || '').length,
        htmlLen: (document.body && document.body.innerHTML || '').length,
        kids: document.body ? document.body.children.length : 0,
        modal: !!document.querySelector('[role=dialog], .modal, .popup, [aria-modal=true]'),
        listbox: document.querySelectorAll('[role=listbox], [role=menu], [role=tree]').length,
        expanded: document.querySelectorAll('[aria-expanded="true"]').length,
        portals: document.querySelectorAll('[data-radix-popper-content-wrapper], [data-headlessui-portal], [data-state="open"]').length,
        focused: document.activeElement ? document.activeElement.tagName + '#' + (document.activeElement.id || '') : ''
    })"""
    try:
        r = await eval_in_tab(ws_url, js)
        return r.get("result", {}).get("value", "")
    except Exception:
        return ""

async def cdp_real_click(ws_url: str, x: float, y: float):
    """Send real OS-level mouse events via CDP Input — bypasses JS event filtering.

    Dispatches the full press sequence: mouseMoved → mousePressed → mouseReleased.
    Both press AND release carry clickCount=1, which is what Chrome needs to
    derive the `click` event. mouseReleased with clickCount=0 (old impl) caused
    `click` to NOT fire on some sites — bug surfaced by Radix/HeadlessUI/
    react-select dropdowns that listen for mousedown.
    """
    for event_type, button_state, click_count in [
        ("mouseMoved",    "none", 0),
        ("mousePressed",  "left", 1),
        ("mouseReleased", "left", 1),
    ]:
        await cdp_send(ws_url, "Input.dispatchMouseEvent", {
            "type": event_type, "x": x, "y": y,
            "button": "left" if button_state != "none" else "none",
            "clickCount": click_count,
            "modifiers": 0,
        })
        await asyncio.sleep(0.05)

async def get_element_center(ws_url: str, text: str) -> tuple[float, float] | None:
    """Get the center coordinates of an element matching text.

    Returns the leaf's center — CDP's hit-test resolves the topmost element
    at that pixel and event bubbling reaches any listener on parents. We do
    NOT lift to an ancestor, because some sites put their click/mousedown
    handler on an intermediate wrapper that a parent-level dispatch would
    bypass (bubbles go up, not down).
    """
    js = f"""(function() {{
        const all = document.querySelectorAll('li, button, a, [role=button], [role=combobox], [role=option], [role=menuitem], [role=link], [aria-haspopup], div, span, label');
        const q = {json.dumps(text.lower())};
        for (const el of all) {{
            if (el.textContent.toLowerCase().includes(q) && el.offsetParent !== null) {{
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return JSON.stringify({{x: r.left + r.width/2, y: r.top + r.height/2}});
            }}
        }}
        return null;
    }})()"""
    result = await eval_in_tab(ws_url, js)
    val = result.get("result", {}).get("value")
    if val:
        try:
            coords = json.loads(val)
            return coords["x"], coords["y"]
        except Exception:
            pass
    return None

async def eval_in_tab(ws_url: str, expression: str) -> dict:
    return await cdp_send(ws_url, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    })

async def screenshot_tab(ws_url: str) -> str:
    result = await cdp_send(ws_url, "Page.captureScreenshot", {"format": "jpeg", "quality": 80})
    return result.get("data", "")

CLICK_JS = """
(function(description) {
    // Widen scan to include modern interactive roles. Radix/HeadlessUI/
    // react-select use combobox/listbox/option/menuitem.
    const all = document.querySelectorAll(
        'button, a, input[type=submit], input[type=button], '
        + '[role=button], [role=link], [role=combobox], [role=option], '
        + '[role=menuitem], [role=tab], [aria-haspopup], [onclick], label, '
        + 'div, span'
    );
    const desc = description.toLowerCase();
    // Fire on the LEAF — events bubble UP, reaching any listener on parents.
    // Lifting to an ancestor breaks listeners on intermediate wrappers (e.g.
    // D2D's listener on .select-component-control, INSIDE the [role=combobox]
    // — lifting to the combobox dispatches above the listener, bubbling never
    // reaches a child). Plus we fire mousedown + mouseup + click + the pointer
    // counterparts — Radix/HeadlessUI listen for one or the other.
    const fire = (el) => {
        try { el.scrollIntoView({block:'center'}); } catch(e) {}
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width/2, cy = r.top + r.height/2;
        const mev = (t) => new MouseEvent(t, {
            bubbles: true, cancelable: true, composed: true,
            view: window, button: 0, buttons: 1, detail: 1,
            clientX: cx, clientY: cy,
        });
        let pev;
        try {
            pev = (t) => new PointerEvent(t, {
                bubbles: true, cancelable: true, composed: true,
                pointerType: 'mouse', pointerId: 1, isPrimary: true,
                button: 0, buttons: 1, clientX: cx, clientY: cy,
            });
        } catch(e) { pev = null; }
        if (pev) el.dispatchEvent(pev('pointerdown'));
        el.dispatchEvent(mev('mousedown'));
        if (pev) el.dispatchEvent(pev('pointerup'));
        el.dispatchEvent(mev('mouseup'));
        el.dispatchEvent(mev('click'));
    };
    // Amazon AUI dispatches handlers through `.a-button` wrappers and
    // `[data-action]` registered targets, not the inner native button. Fire on
    // BOTH the leaf AND any AUI-style action ancestor so either receives the
    // event. Bubbling alone isn't always enough — AUI's declarative system
    // sometimes binds to currentTarget=the-wrapper specifically.
    const auiWrapper = (el) => {
        let cur = el, hops = 0;
        while (cur && cur !== document.body && hops < 6) {
            if (cur.matches && cur.matches('.a-button, [data-action], [data-component-type]')) return cur;
            cur = cur.parentElement;
            hops++;
        }
        return null;
    };
    // Pick the MOST SPECIFIC match (smallest textContent length) — handlers
    // are usually on inner elements; firing on an outer wrapper means the
    // child listener never hears the event (bubbling goes up, not down).
    const matches = [];
    for (const el of all) {
        const text = (el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').toLowerCase();
        if (text.includes(desc)) {
            matches.push({el, textLen: (el.textContent || '').length});
        }
    }
    if (matches.length > 0) {
        matches.sort((a, b) => a.textLen - b.textLen);
        const el = matches[0].el;
        fire(el);
        const w = auiWrapper(el);
        if (w && w !== el) fire(w);
        return 'clicked: ' + (el.textContent || el.tagName).trim().slice(0, 80) + (w && w !== el ? ' (+ AUI wrapper)' : '');
    }
    // CSS selector fallback
    try {
        const el = document.querySelector(description);
        if (el) {
            fire(el);
            const w = auiWrapper(el);
            if (w && w !== el) fire(w);
            return 'clicked by selector: ' + el.tagName + (w && w !== el ? ' (+ AUI wrapper)' : '');
        }
    } catch(e) {}
    return 'not found: ' + description;
})(DESCRIPTION)
"""

FILL_JS = """
(function(fields) {
    // React-aware setter: bypasses React's _valueTracker so controlled
    // inputs accept the value instead of reverting to the previous state.
    const setNative = (el, value) => {
        const proto = (el instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype
                    : (el instanceof HTMLSelectElement)   ? HTMLSelectElement.prototype
                    : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) {
            desc.set.call(el, value);
        } else {
            el.value = value;
        }
    };
    const results = [];
    for (const [key, value] of Object.entries(fields)) {
        const k = key.toLowerCase();
        let found = null;
        // by label
        for (const label of document.querySelectorAll('label')) {
            if (label.textContent.toLowerCase().includes(k)) {
                const input = label.querySelector('input,textarea,select') || document.getElementById(label.htmlFor);
                if (input) { found = input; break; }
            }
        }
        // by placeholder / name / id / aria-label
        if (!found) {
            const sel = `input[placeholder*="${key}" i], input[name*="${key}" i], input[id*="${key}" i], textarea[placeholder*="${key}" i], input[aria-label*="${key}" i]`;
            try { found = document.querySelector(sel); } catch(e) {}
        }
        if (found) {
            found.focus();
            setNative(found, value);
            found.dispatchEvent(new Event('input',  {bubbles:true, composed:true}));
            found.dispatchEvent(new Event('change', {bubbles:true, composed:true}));
            results.push('filled: ' + key);
        } else {
            results.push('not found: ' + key);
        }
    }
    return results.join(', ');
})(FIELDS)
"""

READ_JS = """
(function() {
    const remove = ['script','style','svg','noscript','iframe'];
    const clone = document.body.cloneNode(true);
    remove.forEach(tag => clone.querySelectorAll(tag).forEach(el => el.remove()));
    const title = document.title;
    const url = location.href;
    const text = clone.innerText.replace(/\\s+/g, ' ').trim().slice(0, 8000);
    const inputs = Array.from(document.querySelectorAll('input,textarea,select')).map(el =>
        `[${el.tagName} name="${el.name||''}" placeholder="${el.placeholder||''}" type="${el.type||''}"]`
    ).join('\\n');
    return JSON.stringify({title, url, text, inputs});
})()
"""

# Compressed accessibility-tree scan — returns @eN references that other tools
# can use as selectors (the WebLoom runtime expands @eN → window.__chromeMcpRefs__[N]
# via JS resolution before any DOM query).
SCAN_AX_JS = """
(function() {
    const refs = [];
    const ids = new WeakMap();
    let next = 1;
    const refOf = (el) => {
        if (ids.has(el)) return ids.get(el);
        const r = '@e' + next++;
        ids.set(el, r);
        refs.push(el);
        return r;
    };

    // Selectable predicate — anything a user might click, fill, or read.
    // Skip <label> elements that wrap an input/textarea/select — the input
    // is the actionable target; listing both creates duplicate refs.
    const isSelectable = (el) => {
        if (!el || !el.tagName) return false;
        if (el.tagName === 'LABEL' && el.querySelector('input, textarea, select')) return false;
        if (el.matches && el.matches('button, a[href], [role=button], [role=link], [role=combobox], [role=option], [role=menuitem], [role=tab], [aria-haspopup], [onclick], label, input, textarea, select, [contenteditable=true]')) return true;
        return false;
    };

    const summarize = (el) => {
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || tag;
        const label = (el.getAttribute('aria-label') || '').slice(0, 80);
        const placeholder = (el.getAttribute('placeholder') || '').slice(0, 60);
        const text = ((el.textContent || el.value || '').trim()).slice(0, 80);
        const type = el.type || '';
        const id = el.id || '';
        const name = el.name || '';
        const parts = [refOf(el), '[' + role + (type ? ' type="'+type+'"' : '') + ']'];
        if (label) parts.push('aria-label="' + label + '"');
        else if (text) parts.push('"' + text + '"');
        if (placeholder) parts.push('placeholder="' + placeholder + '"');
        if (id) parts.push('#' + id);
        else if (name) parts.push('name="' + name + '"');
        return parts.join(' ');
    };

    const lines = [];
    const els = document.querySelectorAll('*');
    for (const el of els) {
        if (!isSelectable(el)) continue;
        if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') continue;  // skip hidden
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        lines.push(summarize(el));
        if (lines.length >= 200) break;
    }

    // Stash refs on window for other tools to resolve @eN
    window.__chromeMcpRefs__ = refs;

    return JSON.stringify({
        url: location.href,
        title: document.title,
        lines: lines,
        ref_count: refs.length,
    });
})()
"""

# Resolver: any tool that takes a selector should first run this — if the selector
# starts with @e and a digit, resolve from the window ref cache to a unique CSS path.
def _ax_resolver_prelude(selector: str) -> str | None:
    """Return JS that resolves an @eN selector to a unique CSS path at the moment of execution.
    Returns None if selector doesn't look like an @eN ref."""
    if not (isinstance(selector, str) and selector.startswith("@e") and selector[2:].isdigit()):
        return None
    idx = int(selector[2:]) - 1
    return f"""(function() {{
        const refs = window.__chromeMcpRefs__;
        if (!refs || !refs[{idx}]) return null;
        const el = refs[{idx}];
        // Walk up to build a unique selector
        if (el.id) return '#' + CSS.escape(el.id);
        const path = [];
        let cur = el;
        while (cur && cur !== document.body && path.length < 6) {{
            let part = cur.tagName.toLowerCase();
            if (cur.id) {{ part = '#' + CSS.escape(cur.id); path.unshift(part); break; }}
            if (cur.className) {{
                const cls = (cur.className.toString().trim().split(/\\s+/)[0]);
                if (cls) part += '.' + CSS.escape(cls);
            }}
            const sibs = cur.parentElement ? Array.from(cur.parentElement.children).filter(c => c.tagName === cur.tagName) : [];
            if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
            path.unshift(part);
            cur = cur.parentElement;
        }}
        return path.join(' > ');
    }})()"""


async def resolve_ax_ref(ws_url: str, selector: str) -> str:
    """If selector is @eN, return a real CSS selector. Otherwise return unchanged."""
    js = _ax_resolver_prelude(selector)
    if js is None:
        return selector
    r = await eval_in_tab(ws_url, js)
    val = r.get("result", {}).get("value")
    return val or selector


SCAN_JS = """
(function() {
  function getSelector(el) {
    if (el.id) return '#' + el.id;
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    const cls = Array.from(el.classList).slice(0,2).join('.');
    return el.tagName.toLowerCase() + (cls ? '.' + cls : '');
  }
  const buttons = Array.from(document.querySelectorAll(
    'button, [role=button], input[type=submit], input[type=button], input[type=reset], a[href]'
  )).filter(el => el.offsetParent !== null).map(el => ({
    text: (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 120),
    tag: el.tagName.toLowerCase(),
    type: el.type || el.getAttribute('role') || 'link',
    href: el.href || null,
    selector: getSelector(el),
  })).filter(b => b.text).slice(0, 60);

  const inputs = Array.from(document.querySelectorAll('input, textarea, select')).map(el => {
    const lbl = document.querySelector('label[for="' + el.id + '"]');
    return {
      label: (lbl?.textContent || el.getAttribute('aria-label') || el.placeholder || el.name || '').trim().slice(0, 80),
      name: el.name || el.id || '',
      type: el.type || el.tagName.toLowerCase(),
      placeholder: el.placeholder || '',
      required: el.required,
      selector: getSelector(el),
      value: el.type === 'password' ? '***' : (el.value || '').slice(0, 50),
    };
  }).filter(i => i.label || i.name).slice(0, 40);

  const forms = Array.from(document.querySelectorAll('form')).map(f => ({
    id: f.id, action: f.action, method: f.method,
    fields: Array.from(f.querySelectorAll('input,textarea,select')).map(i => i.name || i.id).filter(Boolean)
  })).slice(0, 10);

  return JSON.stringify({
    url: location.href,
    title: document.title,
    buttons, inputs, forms,
    links_count: document.querySelectorAll('a[href]').length
  });
})()
"""

THREADS_DIR = Path(os.environ.get("WEBLOOM_THREADS", str(Path.home() / ".webloom" / "threads")))
PROJECT_THREADS_DIR = Path(__file__).parent / "threads"  # bundled with the server (built-in default Threads)


def _load_thread_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("domain"):
            return None
        return data
    except Exception:
        return None


def load_threads() -> dict[str, dict]:
    """Discover installable Threads and return {domain: thread_dict}.

    Priority (later wins on key conflict): bundled defaults → user-installed.
    Threads are JSON files with a 'domain' key; other keys are merged into
    the live playbook at read time so WebLoom / WebLoom auto-consults them.
    """
    threads: dict[str, dict] = {}
    for d in (PROJECT_THREADS_DIR, THREADS_DIR):
        if not d.exists():
            continue
        for p in d.glob("*.thread.json"):
            t = _load_thread_file(p)
            if t and t.get("domain"):
                threads[t["domain"]] = t
    return threads


def load_playbook() -> dict:
    """Merged read of live playbook + installed Threads.

    Live playbook (user's accumulated learning) takes priority over Thread
    defaults — user observations override what shipped in the pack. Specifically:
    - Notes are concatenated (Thread first, then live)
    - click_log uses live entries when present, falls back to Thread's
    - default_strategy, quirks, endpoints, selectors etc. use live if set, else Thread
    """
    PLAYBOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    live: dict = {}
    if PLAYBOOK_FILE.exists():
        try:
            live = json.loads(PLAYBOOK_FILE.read_text())
        except Exception:
            live = {}

    threads = load_threads()
    if not threads:
        return live

    merged = {}
    all_domains = set(live.keys()) | set(threads.keys())
    for dom in all_domains:
        t = threads.get(dom, {})
        l = live.get(dom, {})
        # Start from thread, layer live on top
        m = dict(t)
        for k, v in l.items():
            if k == "notes":
                # concat thread notes + live notes, de-dup. Defensive: coerce
                # to list — historical playbook entries may have notes as str.
                def _as_list(x):
                    if x is None:
                        return []
                    if isinstance(x, list):
                        return x
                    return [x]
                seen = set()
                combined = []
                for note in _as_list(t.get("notes")) + _as_list(l.get("notes")):
                    if note not in seen:
                        seen.add(note)
                        combined.append(note)
                m["notes"] = combined
            elif k == "click_log":
                # live wins per-key, but Thread's entries persist for keys live doesn't cover
                merged_log = dict(t.get("click_log", {}) or {})
                merged_log.update(l.get("click_log", {}) or {})
                m["click_log"] = merged_log
            else:
                m[k] = v
        m["_thread_present"] = dom in threads
        merged[dom] = m
    return merged


def _load_live_playbook_raw() -> dict:
    """Read live playbook ONLY (no Thread merging). For write paths — we never
    write merged data back to disk because that would baked Thread content
    into the user's playbook."""
    PLAYBOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PLAYBOOK_FILE.exists():
        try:
            return json.loads(PLAYBOOK_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_playbook_data(playbook: dict):
    PLAYBOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAYBOOK_FILE.write_text(json.dumps(playbook, indent=2))

def domain_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url

def real_tabs(tabs: list) -> list:
    """Filter out extension service workers, offscreen pages, omnibox popups, and chrome:// internals."""
    def is_real(t):
        url = t.get("url", "")
        title = t.get("title", "")
        ttype = t.get("type", "")
        if ttype and ttype != "page":
            return False
        if url.startswith("chrome-extension://"):
            return False
        if url.startswith("chrome://"):
            return False
        if url.startswith("devtools://"):
            return False
        if title.startswith("Service Worker"):
            return False
        if "offscreen" in title.lower():
            return False
        if title.lower() in ("omnibox popup", "omnibox", "new tab"):
            return False
        if url in ("", "about:blank"):
            return False
        return True
    return [t for t in tabs if is_real(t)]


def discover_running_chrome_sessions() -> list[dict]:
    """Find all Chrome processes with a --remote-debugging-port flag."""
    import psutil
    sessions = load_sessions()
    port_to_name = {cfg["port"]: name for name, cfg in sessions.items()}
    found = []
    seen = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "chrome" not in (proc.info["name"] or "").lower():
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            m = re.search(r"--remote-debugging-port=(\d+)", cmdline)
            if not m:
                continue
            port = int(m.group(1))
            if port in seen:
                continue
            seen.add(port)
            tabs = real_tabs(get_tabs(port))
            name = port_to_name.get(port, f"unknown:{port}")
            found.append({
                "name": name,
                "port": port,
                "tab_count": len(tabs),
                "tabs": [t.get("title", "(loading)")[:60] for t in tabs[:4]],
            })
        except Exception:
            pass
    return found

server = Server("webloom")

@server.list_prompts()
async def list_prompts():
    return [
        Prompt(
            name="chrome-start",
            description="Chrome MCP startup — always run this at the start of any browser session",
        )
    ]

@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None):
    if name == "chrome-start":
        return GetPromptResult(
            description="WebLoom startup check",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            "WebLoom is an autonomous browser engine that controls a real logged-in Chrome via CDP. "
                            "Use it when you need real saved logins/sessions, hostile DOM patterns (React/Redux/AUI/Backbone), "
                            "label-wrapped uploads (D2D/KDP), or any site Playwright MCP can't crack.\n\n"
                            "Architecture: WebLoom is the engine. Threads (profile packs) are site-specific knowledge "
                            "stored in playbook + ~/.webloom/threads/. When you scan_tab or click a domain that has an "
                            "installed Thread, WebLoom auto-consults it for default strategy + known quirks.\n\n"
                            "Always call chrome_status first, show the user which sessions are live, and ask which "
                            "session to use before navigating."
                        ),
                    ),
                )
            ],
        )
    raise ValueError(f"Unknown prompt: {name}")

@server.list_tools()
async def list_tools():
    return [
        Tool(name="list_sessions", description="List all configured Chrome sessions and whether they're live", inputSchema={"type": "object", "properties": {}}),
        Tool(name="list_tabs", description="List all open tabs in a Chrome session", inputSchema={"type": "object", "properties": {"session": {"type": "string", "description": "Session name (main, farm, signups, email) or port number"}}, "required": ["session"]}),
        Tool(name="read_tab", description="Read the text content and form fields of a tab", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string", "description": "Tab ID, title substring, or URL substring. Defaults to active tab."}}, "required": ["session"]}),
        Tool(name="screenshot", description="Take a screenshot of a tab — returns an image", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}}, "required": ["session"]}),
        Tool(name="click", description="Click an element by text, aria-label, or CSS selector. Strategy ladder: (Stage 1) actionability wait + CDP isTrusted click → (Stage 2) full JS dispatch sequence (pointer+mouse+click on leaf) → (Stage 3) vision grounding (Claude). WebLoom NEVER moves the OS cursor — there is no Layer 3 / pyautogui path. If all stages fail, returns 'not interactable via DOM' and the caller should hand off to the user. Strategy choice is informed by playbook history (per-domain, per-description success rate ≥ 70% → try that strategy first). 🔁 IF ALL 3 STAGES FAIL — DO NOT GIVE UP. Escalate to: (1) `react_invoke_handler` — walks React fiber to find the element's onClick prop and invokes it directly. Bypasses DOM-event interceptors like LinkedIn's `interop-outlet` overlay that swallows ALL click events at document level. (2) `key_press` Tab+Enter — for menu items/triggers that respond to keyboard but block synthesized mouse. (3) `eval_js` to call the React handler from a known global state (Redux dispatch via `redux_dispatch`, Backbone via `backbone_inspect`). The 'click is blocked' wall almost always falls to fiber-invoke.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "description": {"type": "string", "description": "Text on the button/link, or a CSS selector"}, "allow_real_cursor": {"type": "boolean", "default": False, "description": "DEPRECATED / NO-OP. WebLoom will never move the OS cursor regardless of this flag. Kept only for backward-compat with old callers. Setting true is silently ignored."}, "debug": {"type": "boolean", "default": False, "description": "If true, return a full trace of what each stage tried (actionability probe, dispatch results, verifier diffs, playbook lookup). Use when click reports failure but the page reacted, or to debug strategy selection."}, "timeout_seconds": {"type": "number", "default": 5, "description": "Max time to wait for actionability (element visible+stable+enabled+hit-test) before falling through to JS dispatch."}}, "required": ["session", "description"]}),
        Tool(name="fill", description="Fill form fields. Pass a dict of {label_or_name: value}. 🔁 IF FILL FAILS (controlled input ignores it, value vanishes, no onChange fires): escalate to (1) `react_force_change` — calls the native HTMLInputElement.value setter that React DOES observe, plus dispatches input+change. Solves 90% of React-controlled-input fails. (2) `lexical_set_text` for Lexical editors (Reddit modern, Facebook composer). (3) `draftjs_set_text` for Draft.js (X/Twitter composer, Twilio). (4) `key_type` to type the value char-by-char via CDP keystrokes — last resort but works against anything that listens for real key events. (5) For body-signed APIs (X, TikTok, Instagram), skip DOM entirely and use the per-site protocol tool (e.g. `x_create_tweet`). DO NOT report 'can't fill this field' until the ladder is exhausted.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "fields": {"type": "object", "description": "Key-value pairs of field label/name → value"}}, "required": ["session", "fields"]}),
        Tool(name="eval_js", description="Run arbitrary JavaScript in a tab and return the result", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "code": {"type": "string"}}, "required": ["session", "code"]}),
        Tool(name="navigate", description="Navigate a tab to a URL — waits for Page.loadEventFired (no fixed sleep). Returns when the page has loaded or the timeout elapses.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "url": {"type": "string"}, "timeout_seconds": {"type": "number", "default": 20}}, "required": ["session", "url"]}),
        Tool(name="upload_file", description=(
            "Upload one or more local files to a page. FOUR strategies — pick by site pattern:\n\n"
            "STRATEGY B (default — just pass selector + files): CDP DOM.setFileInputFiles via objectId, pierces shadow DOM + same-origin iframes, re-dispatches input/change events. "
            "Use for plain <input type=file> with vanilla JS listeners. Fails on React-controlled inputs and label-wrapped hidden inputs.\n\n"
            "STRATEGY A (pass click_first_selector): Enables Page file-chooser interception, clicks the visible upload button, feeds the file to the native picker. "
            "Use for sites with a visible 'Browse...' button. "
            "⚠️ Known issue: drops CDP websocket on Windows + Chrome (UI thread blocks on the dialog) — if that happens, fall back to D.\n\n"
            "STRATEGY C (pass drop_target_selector): Reads file bytes server-side, injects real File objects into page JS, fires native dragenter/dragover/drop events on the target. "
            "Use for drag-and-drop zones (react-dropzone, custom dropzones with ondrop listeners). Max 25MB combined.\n\n"
            "STRATEGY D (pass inject_input_selector): Reads file bytes, constructs File in PAGE-JS context, assigns input.files = DataTransfer.files, fires input+change. "
            "Use for label-wrapped hidden inputs (D2D, KDP, Etsy) — the pattern <label><input type=file hidden></label>. "
            "Works on Windows where A drops CDP. Confirmed working on D2D paperback uploads. Max 25MB combined.\n\n"
            "STRATEGY E (pass react_input_selector): Shadow-DOM-aware lookup + native HTMLInputElement.files setter + onChange via fiber walk. "
            "Use for React-controlled file inputs that live inside shadow DOM (LinkedIn composer's interop-outlet pattern, Lit-based SaaS dashboards). "
            "Strategies B and D set the FileList but React's controlled-input observer doesn't notice the synthetic change. "
            "E uses Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files').set — the path React DOES observe — and additionally invokes the input's React onChange via fiber walk for sites that gate on the handler. "
            "Max 25MB combined.\n\n"
            "DECISION TREE: Inside shadow DOM (LinkedIn-style)? → E. Hidden input inside a <label>? → D. Visible drop zone with 'drop files here'? → C. Visible 'Browse' button (and not on Windows)? → A. Plain unhidden input? → B.\n\n"
            "🔁 IF ALL 5 STRATEGIES FAIL — DO NOT GIVE UP. Escalate to: "
            "(1) `xhr_upload` — for SaaS uploaders that intercept input.files and corrupt programmatic uploads (KDP AjaxInput, custom AJAX). "
            "(2) `start_recording` → drive ONE manual upload → `replay_xhr` with new file bytes — captures the exact multipart request shape including CSRF/chunk-id/boundary, then replays forever with any file. Works against ANY uploader that talks HTTP (Bandcamp Plupload, Substack, Notion). "
            "(3) `eval_js` to call the uploader's internal API directly — many old sites expose a global `uploader.addFile()` (Plupload), `window.FU` (Fineuploader), `window.dz` (Dropzone). Walk window for the instance. "
            "(4) CDP drag-synthesis — fire `Input.dispatchDragEvent` with a synthesized DataTransfer carrying file bytes; some sites fire ONLY on `drop`, not on `<input>` change. "
            "(5) `inject_on_new_document` an XHR interceptor that swaps the FormData body of the outgoing multipart request — network-level file substitution before the server sees it. "
            "If 1–5 ALL fail, that's the moment to capture findings to a Thread and pause for human — not earlier. Site-wall claims need to exhaust this whole ladder first.\n"
            "Always pass absolute file paths."
        ), inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "selector": {"type": "string", "description": "CSS selector for the <input type=file> element (used by Strategy B). Required even when using Strategy A — pass any sentinel like 'input[type=file]' as a fallback."}, "files": {"type": "array", "items": {"type": "string"}, "description": "Absolute paths to local files"}, "click_first_selector": {"type": "string", "description": "Strategy A: CSS selector for the VISIBLE upload button that opens a native file picker. Clicks it and intercepts the file-chooser dialog. NOTE: on Windows + Chrome, file-chooser interception can drop the CDP websocket — if it does, fall back to drop_target_selector (Strategy C)."}, "drop_target_selector": {"type": "string", "description": "Strategy C: CSS selector for a drag-and-drop zone. Reads file bytes server-side, injects them as real File objects in JS, and fires native dragenter/dragover/drop events on the target. Works against React-controlled inputs that ignore programmatic FileList changes. Max 25MB combined."}, "inject_input_selector": {"type": "string", "description": "Strategy D: CSS selector for the hidden <input type=file>. Reads file bytes, constructs File objects in PAGE-JS context, assigns via DataTransfer.files to input.files, then fires input + change events. Solves label-wrapped-hidden-input designs (D2D, KDP) where file picker intercept drops CDP on Windows AND no drop listener exists. Max 25MB combined."}, "react_input_selector": {"type": "string", "description": "Strategy E: CSS selector for a React-controlled file input, possibly inside shadow DOM. Walks shadow roots recursively to find the input, builds File objects in page JS, assigns via the native HTMLInputElement.files prototype setter (which React's controlled-input observer DOES notice — unlike B/D's synthetic path), fires input+change, AND invokes the input's onChange via fiber walk for handler-gated implementations. Use for LinkedIn composer + similar Lit/shadow-DOM React patterns. Max 25MB combined."}}, "required": ["session", "selector", "files"]}),
        Tool(name="new_tab", description="Open a new tab in a session (uses Target.createTarget — works in all Chrome versions)", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "url": {"type": "string", "default": "about:blank"}}, "required": ["session"]}),
        Tool(name="wait_for", description="Wait for an element to appear in a tab (polls every 500ms)", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "selector": {"type": "string"}, "timeout_ms": {"type": "integer", "default": 10000}}, "required": ["session", "selector"]}),
        Tool(name="launch_session", description="Connect to or launch a Chrome debug session. ALWAYS call with no arguments first — it will list available sessions and pause. You MUST show that list to the user and wait for them to pick a session before calling again. Only set confirmed_by_user=true after the user has explicitly told you which session to use. Never guess or auto-select.", inputSchema={"type": "object", "properties": {"session": {"type": "string", "description": "Session name from config: main, farm, signups, email. Leave empty to list available sessions."}, "url": {"type": "string", "description": "Optional URL to open if launching new"}, "force_new": {"type": "boolean", "default": False, "description": "Set true to launch a new Chrome window even if one is already running"}, "confirmed_by_user": {"type": "boolean", "default": False, "description": "Set true ONLY after you have shown the session list to the user and they have explicitly chosen a session. Never set this without user confirmation."}}, "required": []}),
        Tool(name="scan_tab", description="Page intelligence scan. Modes: 'ax' (default — compressed accessibility tree with @eN refs, ~200-400 tokens, every interactive element gets a stable @e1/@e2/etc handle you can pass to click/fill/key_type/upload as the selector); 'full' (legacy — every button/input/form with CSS selectors, ~5000 tokens, good for unfamiliar sites where you need everything). Call before any unfamiliar interaction. Auto-prepends playbook knowledge for the domain.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "mode": {"type": "string", "enum": ["ax", "full"], "default": "ax", "description": "ax = compressed accessibility tree with @eN refs (cheap, default). full = legacy verbose scan with CSS selectors."}, "save_to_playbook": {"type": "boolean", "default": True, "description": "Auto-save discovered structure to playbook for this domain (full mode only)"}}, "required": ["session"]}),
        Tool(name="get_playbook", description="Read everything Claude has learned about a domain — known quirks, field names, working patterns, affiliate links. Includes data merged from any installed Threads (profile packs).", inputSchema={"type": "object", "properties": {"domain": {"type": "string", "description": "Domain like bitget.com or mexc.com. Leave empty to list all known domains."}}, "required": []}),
        Tool(name="list_threads", description="List installable Threads (profile packs) currently discovered by WebLoom. Shows domain, version, source path, and which keys it contributes.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="install_thread", description="Install a Thread (profile pack) into ~/.webloom/threads/. Pass a local file path to a .thread.json. WebLoom auto-merges the Thread's knowledge into the playbook on the next read — no restart needed for the read path, but tools loaded at startup (like click handler defaults) will need a session restart to fully pick it up.", inputSchema={"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to a .thread.json file"}, "overwrite": {"type": "boolean", "default": False, "description": "Overwrite if a Thread for this domain is already installed"}}, "required": ["path"]}),
        Tool(name="export_thread", description="Export the live playbook entries for a domain as a portable Thread (.thread.json). Useful for: (a) saving accumulated learning as a sellable pack, (b) sharing site knowledge across machines, (c) backing up before destructive playbook edits. Returns the file path where it was written.", inputSchema={"type": "object", "properties": {"domain": {"type": "string", "description": "Domain to export (e.g. 'kdp.amazon.com')"}, "out_path": {"type": "string", "description": "Optional output path. Defaults to ~/.webloom/threads/<domain>.thread.json"}, "name": {"type": "string", "description": "Friendly name for the Thread (e.g. 'KDP Auto-Publish')"}, "version": {"type": "string", "default": "1.0.0"}, "author": {"type": "string"}, "license": {"type": "string", "default": "proprietary"}}, "required": ["domain"]}),
        Tool(name="save_playbook", description="Save a discovered pattern, quirk, or working recipe for a domain so Claude remembers it forever", inputSchema={"type": "object", "properties": {"domain": {"type": "string"}, "key": {"type": "string", "description": "What this is: 'login_fields', 'affiliate_url', 'quirks', 'signup_flow', etc."}, "value": {"description": "The value — string, list, or object"}}, "required": ["domain", "key", "value"]}),
        Tool(name="note", description="Add a freeform note for a domain — tips, warnings, timing quirks, things to remember. Auto-shows when you scan that domain.", inputSchema={"type": "object", "properties": {"domain": {"type": "string", "description": "e.g. bitget.com, mexc.com"}, "text": {"type": "string", "description": "The note — e.g. 'wait 1.5s after login before clicking dashboard' or 'reCAPTCHA fires on step 3, hand off to user'"}}, "required": ["domain", "text"]}),
        Tool(name="list_running_chrome", description="Discover all currently running Chrome instances with debug ports — named sessions and unknown ones.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="confirm_login", description="Record the user's answer to the 'Are you logged in?' question for a given session. Only call this AFTER asking the user via AskUserQuestion and getting their explicit answer. Never call with logged_in=true unless the user said yes.", inputSchema={"type": "object", "properties": {"session": {"type": "string", "description": "Session name the user answered for."}, "logged_in": {"type": "boolean", "description": "True if user confirmed they are logged in, false if they still need to log in."}}, "required": ["session", "logged_in"]}),
        Tool(name="chrome_status", description="CALL THIS FIRST — full status: which sessions are live, which are offline, what tabs are open, and what to do next. Always call before any Chrome MCP work.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="scroll_tab", description="Scroll a Chrome tab — useful for pages where content loads below the fold", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "direction": {"type": "string", "enum": ["down", "up", "left", "right"], "default": "down"}, "amount": {"type": "integer", "default": 500, "description": "Pixels to scroll"}}, "required": ["session"]}),
        Tool(name="close_tab", description="Close a specific tab in a Chrome session", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string", "description": "Tab title, URL substring, or ID"}}, "required": ["session", "tab"]}),
        Tool(name="find_tab_by_selector", description="Scan all tabs in a session and return the first tab where a JS expression returns truthy. Use this when multiple tabs share the same URL and you need the one with the right DOM state. Example: js_test='!!document.querySelector(\"button\")' or js_test='document.title.includes(\"loaded\")'", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "js_test": {"type": "string", "description": "JS expression evaluated in each tab — return the tab where this is truthy"}, "url_filter": {"type": "string", "description": "Optional URL substring to skip irrelevant tabs"}}, "required": ["session", "js_test"]}),
        Tool(name="start_recording", description="Start recording a browser workflow into a named recipe. Every click/fill/navigate/wait_for after this is logged. Call end_recording when done to save. Use parameters dict during replay to substitute {{var}} placeholders in args.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Recipe name (alphanumeric, dash, underscore)"}, "goal": {"type": "string", "description": "Plain-English description of what this recipe accomplishes"}, "domain": {"type": "string", "description": "Optional domain hint, e.g. mail.google.com"}}, "required": ["name"]}),
        Tool(name="end_recording", description="Save the in-progress recording as a recipe. Use outcome='failed' to abort without saving. parameters lists variable names that appeared in args (e.g. ['email','password']) — they can be passed as a dict during replay_recipe.", inputSchema={"type": "object", "properties": {"outcome": {"type": "string", "enum": ["success", "failed", "abort"], "default": "success"}, "parameters": {"type": "array", "items": {"type": "string"}, "description": "Names of variables that should be substituted at replay time"}}, "required": []}),
        Tool(name="list_recipes", description="List all saved browser-workflow recipes with their goals and action counts.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="replay_recipe", description="Replay a saved recipe — runs every recorded action in sequence with retry + abort-on-failure. Pass parameters dict to substitute {{var}} placeholders captured during recording.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Recipe name to replay"}, "parameters": {"type": "object", "description": "Key-value pairs for {{var}} substitution, e.g. {'email': 'foo@bar.com', 'password': 'secret'}"}, "session": {"type": "string", "description": "Optional session override (recipes don't store session — they replay against whatever you pass here)"}, "tab": {"type": "string", "description": "Optional tab override"}}, "required": ["name"]}),
        Tool(name="detect_blocker", description="Detect login walls, CAPTCHA (reCAPTCHA / hCaptcha / Cloudflare / DataDome / Arkose), 2FA prompts (TOTP / SMS / push / hardware key), and verification challenges on the current tab. Call before any flow that might hit one.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}}, "required": ["session"]}),
        Tool(name="detect_anti_bot", description="Detect anti-bot interception. Returns the type (cloudflare_challenge / datadome / perimeterX / arkose / akamai / empty_shell / normal) plus signals. Different from detect_blocker — that's CAPTCHA/2FA for users; this is silent bot detection that returns a stub page. Use after navigation to know if the page actually loaded or if you hit a challenge.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}}, "required": ["session"]}),
        Tool(name="framework_detect", description="Identify which JS frameworks the current page uses. Returns React (with version), Redux, AUI, Backbone, Vue, Angular, Radix, HeadlessUI, Svelte, Next.js, Nuxt, Amazon P-loader, plus DOM indicators (password input, file input, label-wrapped file, drop zone, iframe count). Use to pick the right strategy upfront — e.g. AUI detected → reach for aui_dispatch; Redux detected → react_inspect_store.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}}, "required": ["session"]}),
        Tool(name="wait_for_idle", description="Wait until the page reaches Page.lifecycleEvent 'networkAlmostIdle' — the proper signal that an SPA has finished hydrating and network activity has settled. Use after navigate() for React/Next/Nuxt apps where readyState=complete fires before content is rendered.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "timeout_seconds": {"type": "number", "default": 10}}, "required": ["session"]}),
        Tool(name="seed_from_tab", description="Build a starter Thread for the current tab's domain by combining framework_detect + scan_tab + detect_anti_bot + optional network capture. Returns a Thread JSON the agent can review, edit, or directly export via export_thread. Lets ANY Claude session contribute Threads to the marketplace just by visiting a site.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "capture_seconds": {"type": "number", "default": 0, "description": "If > 0, captures network for this many seconds during analysis to discover endpoint URLs. Adds capture data to the Thread."}, "save": {"type": "boolean", "default": False, "description": "If true, also exports the Thread to ~/.webloom/threads/<domain>.thread.json"}}, "required": ["session"]}),
        Tool(name="auth_totp", description="Fill a TOTP one-time code from a base32 secret into the page's OTP input. Provide the secret directly (e.g. retrieved from Credential Guardian vault out-of-band). Optionally specify a CSS selector; otherwise auto-detects input[autocomplete=one-time-code] / name=otp / name=2fa.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "secret": {"type": "string", "description": "Base32 TOTP secret (the 'shared secret' shown when setting up Google Authenticator / Authy)"}, "selector": {"type": "string", "description": "Optional CSS selector for the OTP field. If empty, auto-detect."}, "submit": {"type": "boolean", "default": False, "description": "If true, also press Enter / submit after filling."}}, "required": ["session", "secret"]}),
        Tool(name="pause_for_human", description="Pause an automated flow to hand off to the user — for CAPTCHA, push 2FA, hardware keys, or any step that legally requires a human. Returns a structured 'paused' message and plays a system sound. Caller should AskUserQuestion to confirm resume.", inputSchema={"type": "object", "properties": {"reason": {"type": "string", "description": "Why we're pausing — e.g. 'reCAPTCHA on bitget.com login', 'Duo push approval needed'"}, "instructions": {"type": "string", "description": "Plain-English instructions for what the user should do"}, "beep": {"type": "boolean", "default": True}}, "required": ["reason"]}),
        Tool(name="export_profile", description="Export all cookies and storage for a session to a JSON file — for portability across machines or as a session backup. Sensitive: do NOT commit the output to git.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "out_path": {"type": "string", "description": "Absolute path to write the JSON file"}}, "required": ["session", "out_path"]}),
        Tool(name="import_profile", description="Import cookies from a JSON file (previously exported via export_profile) into a session. The session must be live.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "in_path": {"type": "string", "description": "Absolute path to the cookie JSON file"}}, "required": ["session", "in_path"]}),
        Tool(name="capture_network_start", description="Start capturing network requests on a tab. Records URL, method, status, mimeType, request/response timing. Returns immediately; capture continues in the background until capture_network_stop.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}}, "required": ["session"]}),
        Tool(name="capture_network_stop", description="Stop network capture and return all captured requests for the tab. Pass full=true to get headers + request body (required for replay_xhr). Default returns a one-line summary per request.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "url_filter": {"type": "string", "description": "Optional substring — only return requests whose URL contains this"}, "full": {"type": "boolean", "description": "If true, return full request_headers, request_body, response_headers as JSON. Use when you need to replay (auth tokens, csrf, transaction-ids, body shape)."}}, "required": ["session"]}),
        Tool(name="get_captured_requests", description="Get current captured network requests for a tab without stopping capture. Pass full=true to get headers + request body.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "url_filter": {"type": "string"}, "full": {"type": "boolean", "description": "If true, return full headers + body — needed for replay_xhr."}}, "required": ["session"]}),
        Tool(name="scan_tab_diff", description="Diff-scan: returns ONLY buttons/inputs/forms that appeared, disappeared, or changed since the last scan_tab or scan_tab_diff on this tab. Massively reduces tokens on multi-step flows. First call returns the full scan.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}}, "required": ["session"]}),
        Tool(name="key_type", description="Type a string into the focused element. Two modes: 'keystrokes' (default) fires per-char keyDown+keyUp with full keyboard metadata (windowsVirtualKeyCode, code, text, unmodifiedText) — what Playwright does, triggers React onChange/Backbone listeners. 'insertText' is faster, IME-friendly, but some legacy/hostile sites (D2D, older React/Backbone) don't see keypress events from it. Tip: click an input first to focus, then key_type. If saved values come out as [object Object] or look corrupted, try react_force_change as an escape hatch.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "text": {"type": "string", "description": "Text to type"}, "delay_ms": {"type": "integer", "default": 30, "description": "Delay between keystrokes"}, "mode": {"type": "string", "enum": ["keystrokes", "insertText", "fast"], "default": "keystrokes", "description": "keystrokes = per-char keyDown+keyUp (most compatible, default). insertText = single CDP call per char (faster, IME-friendly, less compatible with old React). fast = set value once on focused input + dispatch input/change (fastest, only works for non-React/non-Backbone vanilla forms)."}}, "required": ["session", "text"]}),
        Tool(name="react_force_change", description="Escape hatch for hostile React/Backbone inputs where neither fill nor key_type triggers proper state update (D2D 'Add New Author' [object Object] bug). Walks the input element's React fiber and INVOKES the memoized onChange handler directly with a synthetic event carrying the value. Bypasses keyboard dispatch entirely. Last-resort — use only when key_type produces a corrupted saved value.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "selector": {"type": "string", "description": "CSS selector for the input element"}, "value": {"type": "string", "description": "String value to set"}}, "required": ["session", "selector", "value"]}),
        Tool(name="react_inspect_store", description="Find and inspect a page's Redux store / React Context providers. Discovery paths: (1) window.__REDUX_DEVTOOLS_EXTENSION__ presence + connected stores, (2) window.store / window.__store__ globals, (3) walk React fiber from a selector (or body) looking for a Provider with store.dispatch + store.getState. Returns store presence, current state snapshot (truncated), and Provider tree summary. Use when react_force_change writes the input but the page reads from Redux/Context state outside the fiber chain.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "from_selector": {"type": "string", "description": "Optional CSS selector to start the fiber walk from (default: document.body). Use the closest element to the form/component whose state you want to access."}, "max_state_chars": {"type": "integer", "default": 4000, "description": "Truncate the state snapshot in the return value (Redux stores can be huge)."}}, "required": ["session"]}),
        Tool(name="redux_dispatch", description="Dispatch a Redux action against a page's discovered store. Uses the same store-discovery logic as react_inspect_store. Returns the new state after dispatch (truncated). Use when you need to programmatically force state changes for forms/modals whose state lives outside the React fiber chain (D2D new-author save, complex multi-step forms backed by Redux).", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "action": {"type": "object", "description": "Redux action object — must have a 'type' string and optionally a 'payload'. Example: {type: 'AUTHOR_CREATE', payload: {name: 'Confluence of Wisdom Editorial'}}"}, "from_selector": {"type": "string", "description": "Optional CSS selector for fiber-walk store discovery"}, "max_state_chars": {"type": "integer", "default": 4000}}, "required": ["session", "action"]}),
        Tool(name="aui_dispatch", description="Inspect or invoke Amazon's AUI (A) declarative event system — for KDP, Amazon Seller, AWS Console, anything built on Amazon's AUI framework. With no 'event': lists available A.state stores, registered data-action handlers near a target selector, and AUI module presence. With 'event' + 'target_selector': invokes A.declarative.fire(event, target) — bypasses click handlers and goes straight to the declarative handler. Crack pattern for KDP modal save buttons that don't persist state via normal clicks.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "event": {"type": "string", "description": "Declarative event name (e.g. 'a:click', 'a:save', 'click'). Leave empty for inspect-only mode."}, "target_selector": {"type": "string", "description": "CSS selector for the element to fire the event on (required when event is set, optional in inspect mode to scope handler listing)."}, "payload": {"type": "object", "description": "Optional payload object passed to the handler."}}, "required": ["session"]}),
        Tool(name="backbone_inspect", description="Inspect a page's Backbone.js objects — for sites built on Backbone (legacy Amazon properties, some older KDP pages). Reports presence of Backbone, history routes, registered models/collections, and current route. Read-only for now; if useful we can add backbone_invoke later for triggering router actions.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "max_chars": {"type": "integer", "default": 4000}}, "required": ["session"]}),
        Tool(name="lexical_set_text", description=(
            "Set plain text inside a Lexical-based contenteditable (Reddit's new composer, Notion-like editors, Meta apps). "
            "LEXICAL ONLY — DO NOT USE FOR DRAFT.JS SITES (X/Twitter composer). Use draftjs_set_text instead for those. "
            "Standard input strategies desync because Lexical maintains its own state model separate from the DOM. "
            "This tool: (1) optionally clicks a placeholder to mount the editor and polls for the contenteditable; "
            "(2) accesses Lexical's exposed __lexicalEditor instance via a fiber-walk; "
            "(3) if found, uses setEditorState(parseEditorState(...)) to set a clean serialized state with the text — bypasses DOM entirely; "
            "(4) if no editor handle is reachable, falls back to focus + selectAll + delete + native paste event with proper DataTransfer; "
            "(5) verifies final innerText length and returns a sample. Returns {ok, mode, readback_len, sample}. "
            "If readback_len is 0 on a target you suspect is Draft.js (X.com), switch to draftjs_set_text."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"},
            "tab": {"type": "string"},
            "container_selector": {"type": "string", "description": "CSS selector for the contenteditable OR a wrapper that contains it. Walks down to find [contenteditable=true]."},
            "text": {"type": "string", "description": "Plain text (markdown allowed; Reddit's composer will render shortcuts)."},
            "click_placeholder_selector": {"type": "string", "description": "Optional: selector for a placeholder element to click first (triggers Lexical mount on Reddit's lazy composer). After click, polls for contenteditable up to mount_timeout_seconds."},
            "mount_timeout_seconds": {"type": "number", "default": 5, "description": "How long to wait for the contenteditable to appear after clicking placeholder."},
            "submit_via_enter": {"type": "boolean", "default": False, "description": "After setting text, fire Enter on the editor to submit (some single-line composers)."}
        }, "required": ["session", "container_selector", "text"]}),
        Tool(name="draftjs_set_text", description=(
            "Set plain text inside a Draft.js-based contenteditable (X/Twitter composer). "
            "Draft.js maintains EditorState as the source of truth (NOT the DOM), and its handlePastedText "
            "checks isTrusted, so synthetic paste/compositionend events fail to update state — DOM gets the "
            "text but the post button stays disabled. "
            "This tool runs four strategies in order: (1) walks the React fiber from the contenteditable looking "
            "for an editor instance with props.editorState + props.onChange, calls onChange with a fresh state "
            "built from the editor's own Modifier when reachable; (2) falls back to beforeinput InputEvent with "
            "inputType='insertText' which Draft.js's keypress handler accepts as a real typing event; "
            "(3) falls back to REAL CDP KEYSTROKES via Input.dispatchKeyEvent with proper keyDown/keyUp pairs + "
            "the `text` field per char — this is the same shape Chrome produces from real human typing, which "
            "Draft.js's keypress handler accepts and routes through its proper state update. Preceded by a focus-"
            "settle ritual (real click → 350ms wait → space + backspace warmup → 100ms wait). "
            "NOT Input.insertText (which is IME composition mode — Draft.js drops/commits unpredictably at "
            "non-human cadences). delay_ms default 80; bump to 120-150 if char drops persist on slow machines, "
            "or pass verify_per_char=true for bulletproof per-char retry. "
            "Returns {ok, mode, readback_len, dropped_chars, sample}. Use after lexical_set_text returns "
            "readback_len 0 on a Draft.js target."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"},
            "tab": {"type": "string"},
            "container_selector": {"type": "string", "description": "CSS selector for the contenteditable or its wrapper. For X.com: [data-testid='tweetTextarea_0']."},
            "text": {"type": "string", "description": "Plain text to set."},
            "click_first": {"type": "boolean", "default": True, "description": "Real CDP click on the editor before typing (focus). Almost always wanted."},
            "submit_via_enter": {"type": "boolean", "default": False, "description": "After setting text, fire Enter (some compose flows submit on Enter)."},
            "delay_ms": {"type": "integer", "default": 80, "description": "Milliseconds between keystrokes in strategy 3 (CDP keystrokes). Default 80 (safe). Bump to 120-150 if char drops occur on slow machines; drop to 40-60 only on fast clean ones. Lower = faster but risks Draft.js missing events."},
            "verify_per_char": {"type": "boolean", "default": False, "description": "If true, read composer.innerText after each char and retry dropped chars up to 3 times. Bulletproof but adds ~30ms CDP roundtrip per char. Use for high-stakes single shots; leave off for normal posting."}
        }, "required": ["session", "container_selector", "text"]}),
        Tool(name="vision_check", description=(
            "Vision fallback for when DOM strategies can't reach an element (canvas-rendered, weird custom widgets, "
            "OAuth popups, iframed UIs). Takes a screenshot of the current tab, sends it to Claude with your question, "
            "returns the answer + optional click coordinates. Use when scan_tab returns no useful selectors or click() "
            "keeps missing the target. Returns {answer, click: {x,y} | null}. If coordinates returned, you can pass "
            "them to a CDP click via click_at_coords."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"},
            "tab": {"type": "string"},
            "question": {"type": "string", "description": "Plain-English question. Examples: 'where is the post button?', 'is there a CAPTCHA visible?', 'what does the error message say?'. If asking for a click target, phrase as 'click coords for X' so the model returns coordinates."},
            "include_coords": {"type": "boolean", "default": True, "description": "Ask the model to return {x, y} coordinates when the question is locating a UI element."}
        }, "required": ["session", "question"]}),
        Tool(name="click_at_coords", description="Real CDP click at absolute (x, y) viewport coordinates. Pair with vision_check when you have coords but no selector. Lower-level than click().", inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "x": {"type": "number"}, "y": {"type": "number"},
            "double": {"type": "boolean", "default": False}
        }, "required": ["session", "x", "y"]}),
        Tool(name="enable_stealth", description=(
            "Apply stealth patches to the current tab — masks navigator.webdriver, plugins, languages, WebGL vendor, "
            "permissions API, and chrome runtime to defeat the most common Cloudflare/Akamai/Datadome fingerprinting. "
            "Real Chrome sessions usually don't need this (they ARE real), but use it on fresh-profile flows or sites "
            "that block CDP-attached Chrome. Persistent across navigations (uses inject_on_new_document)."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"}
        }, "required": ["session"]}),
        Tool(name="run_parallel", description=(
            "Run multiple tool calls in parallel across tabs or sessions. Each item is {tool, args}. Returns a list of "
            "results in the same order. Useful for: fanning out a check across N tabs (preflight all your Threads at once), "
            "driving multiple Chrome sessions in lockstep (e.g. cross-poster), or just batching independent CDP work to "
            "save wall time. Skips ordering guarantees — each call is independent."
        ), inputSchema={"type": "object", "properties": {
            "calls": {"type": "array", "description": "List of {tool, args} entries to run concurrently.", "items": {"type": "object", "properties": {
                "tool": {"type": "string"},
                "args": {"type": "object"}
            }, "required": ["tool", "args"]}},
            "max_concurrency": {"type": "integer", "default": 4, "description": "Max parallel calls in-flight. Default 4."}
        }, "required": ["calls"]}),
        Tool(name="solve_captcha", description=(
            "Submit a CAPTCHA challenge to a third-party solver (2captcha or capmonster) and return the token. "
            "Currently supports reCAPTCHA v2/v3 + hCaptcha + Cloudflare Turnstile. Requires "
            "CAPTCHA_PROVIDER ('twocaptcha' or 'capmonster') and CAPTCHA_API_KEY env vars. "
            "Without keys returns ok:false + a hint to use pause_for_human. Costs money per solve (~$0.001-0.003)."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "type": {"type": "string", "enum": ["recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile"], "description": "Challenge type"},
            "site_key": {"type": "string", "description": "data-sitekey or data-site-key attribute from the captcha widget"},
            "page_url": {"type": "string", "description": "URL the captcha appears on. Omit to use current tab url."},
            "action": {"type": "string", "description": "reCAPTCHA v3 action name (optional)"},
            "min_score": {"type": "number", "description": "reCAPTCHA v3 min_score (default 0.3)"}
        }, "required": ["session", "type", "site_key"]}),
        Tool(name="check_thread_updates", description=(
            "Manually trigger the auto-update sweep that the engine runs every 6h automatically. "
            "For each Thread in ~/.webloom/threads/, polls webloom.run for a newer version (an admin-approved "
            "patch from another buyer's drift report). If found, atomically overwrites the local Thread file. "
            "Returns {checked, updated, errors, updates:[{domain, from_version, to_version, patches}]} so you "
            "can see exactly what was patched. Opt out entirely via env WEBLOOM_AUTO_UPDATE=off."
        ), inputSchema={"type": "object", "properties": {}}),
        Tool(name="show_recent_auto_heals", description=(
            "Show the most recent auto-update events from ~/.webloom/auto_updates.jsonl. Each entry shows the "
            "domain, the version bump, and a summary of the patches that landed. Transparency tool — buyers can "
            "see exactly what changed in their Threads and when, even though the updates happen silently in the "
            "background. Returns {events: [...], total_logged}."
        ), inputSchema={"type": "object", "properties": {
            "limit": {"type": "integer", "default": 10}
        }}),
        Tool(name="react_invoke_handler", description=(
            "Bypass DOM event interceptors by calling a React component's prop handler directly. "
            "Use when normal click() / dispatch-events / vision_click all fail because a custom-element overlay "
            "(LinkedIn's interop-outlet, some Web Components wrappers) is eating mouse events at the DOM layer. "
            "Walks __reactFiber / __reactProps to find an onClick / onSubmit / onChange / onPointerDown handler "
            "and invokes it with a synthetic event object. This works because React props live on the component "
            "instance, not in the DOM event chain — overlays can't intercept what isn't dispatched. "
            "Returns {ok, via, hops, handler_kind}. The fiber walk also handles wrappers (button inside a span "
            "inside an interop-outlet) by climbing up to 20 hops until it finds the handler."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "selector": {"type": "string", "description": "CSS selector for the element you'd normally click. The fiber walk starts here and climbs."},
            "handler": {"type": "string", "default": "onClick", "description": "Which prop handler to invoke. Common: onClick, onSubmit, onMouseDown, onPointerDown, onChange, onKeyDown."},
            "event_payload": {"type": "object", "description": "Optional fields to add to the synthetic event object (e.g. {key:'Enter', keyCode:13} for onKeyDown)."}
        }, "required": ["session", "selector"]}),
        Tool(name="swarm_run", description=(
            "Multi-agent swarm — two specialist Claude agents cooperate on one Chrome session to accomplish a "
            "goal. The DRIVER plans and executes actions. The WATCHER monitors for popups, captchas, error modals, "
            "session expiry every few seconds via vision_check and signals the driver to pause/handle. "
            "Use for long high-stakes flows that need babysitting (multi-step publish wizards, complex onboardings, "
            "anything where a single-agent run would need human intervention mid-flow). "
            "ALSO doubles as a Thread-authoring accelerator: pass emit_thread=true and the captured driver_actions "
            "+ verified successes are returned as a proposed Thread JSON skeleton you can review + publish. "
            "Requires ANTHROPIC_API_KEY. Costs ~$0.15-0.40 per session on the user's key (3-4x single-agent cost). "
            "Runs synchronously for up to max_minutes; returns full transcript on completion/timeout."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"},
            "tab": {"type": "string"},
            "goal": {"type": "string", "description": "Plain-English goal for the swarm. Example: 'Publish a new book on KDP using book.docx and cover.jpg'."},
            "thread_domain": {"type": "string", "description": "Optional domain hint — the swarm pulls relevant Thread context from the playbook (proven_actions, preflight, notes, anti-bot signals) for this domain."},
            "max_minutes": {"type": "integer", "default": 8, "description": "Hard cap on swarm runtime. Default 8 min."},
            "max_steps": {"type": "integer", "default": 15, "description": "Hard cap on driver actions. Default 15."},
            "emit_thread": {"type": "boolean", "default": False, "description": "If true, after a successful run, return a proposed Thread JSON skeleton with the captured proven_actions."},
            "watcher_interval_seconds": {"type": "number", "default": 4.0, "description": "How often the watcher inspects the screen. Lower = more responsive, more tokens. Default 4s."}
        }, "required": ["goal"]}),
        Tool(name="visual_diff_preflight", description=(
            "Snap a small screenshot region of a target element and either (a) store the perceptual hash + "
            "neighborhood rgb fingerprint as a 'visual anchor' on first run, or (b) compare against the stored "
            "anchor to detect visual drift even when the selector still exists. "
            "Use when you have a Thread step where the BUTTON CHANGED LOOK but the selector survived "
            "(CSS refresh, dark-mode toggle, icon swap). Returns {ok, mode: 'recorded' | 'compared', similarity, drift_detected}. "
            "Hashes locally — zero AI cost, no external API."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "selector": {"type": "string", "description": "CSS selector for the element to anchor."},
            "anchor_name": {"type": "string", "description": "Friendly name to identify this anchor in the playbook (e.g. 'tweet_post_button')."},
            "threshold": {"type": "number", "default": 0.85, "description": "Similarity below this triggers drift_detected=true. Range 0..1, default 0.85."},
            "mode": {"type": "string", "enum": ["auto", "record", "compare"], "default": "auto", "description": "auto = record on first call for this anchor, compare on subsequent."}
        }, "required": ["session", "selector", "anchor_name"]}),
        Tool(name="weave", description=(
            "Natural-language compose layer over Threads. Given a plain-English goal (e.g. 'publish my new book to "
            "KDP and Draft2Digital using book.md and cover.jpg'), the engine plans an action sequence using your "
            "installed Threads as the building blocks and executes it. Calls Claude Haiku for the planning step "
            "(uses ANTHROPIC_API_KEY from env). Falls back to listing the relevant Threads if no AI key. "
            "Returns {ok, plan: [...], result_summary}. Plan items are tool calls — caller can dry_run them."
        ), inputSchema={"type": "object", "properties": {
            "goal": {"type": "string", "description": "Plain-English goal."},
            "context": {"type": "object", "description": "Optional structured context (files, urls, preferences) the planner can reference."},
            "dry_run": {"type": "boolean", "default": False, "description": "If true, only return the plan without executing."},
            "session": {"type": "string", "default": "default", "description": "Session to execute the plan against."}
        }, "required": ["goal"]}),
        Tool(name="subscribe_to_websocket", description=(
            "Listen for WebSocket messages on the current tab matching a pattern. Use for real-time sites "
            "(Slack, Discord, Reddit chat, dashboards) where polling DOM is wasteful. Pure CDP — wraps "
            "Network.webSocketFrameReceived. Returns immediately; matching frames buffer in memory and you "
            "drain them via poll_websocket_messages. Pure local — zero AI cost."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "pattern": {"type": "string", "description": "Substring or regex (start with 'regex:') to match against incoming frame payloads."},
            "buffer_id": {"type": "string", "description": "Identifier you'll use with poll_websocket_messages to drain buffered matches. Pick any string."},
            "max_buffer": {"type": "integer", "default": 100, "description": "Max messages to buffer before dropping oldest."}
        }, "required": ["session", "pattern", "buffer_id"]}),
        Tool(name="poll_websocket_messages", description=(
            "Drain buffered WebSocket messages matched by a previous subscribe_to_websocket call. Returns "
            "{messages: [...], remaining_in_buffer}. Each message has {ts, opcode, payload}."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "buffer_id": {"type": "string"},
            "max": {"type": "integer", "default": 50}
        }, "required": ["session", "buffer_id"]}),
        Tool(name="episodic_remember", description=(
            "Write a small note about the current session's state on a domain — for example 'paused at KDP "
            "step 3, book ID X, cover not yet uploaded'. The engine stores these locally at "
            "~/.webloom/episodic/<domain>.json so the NEXT session can resume. Pure local, zero AI cost. "
            "Use sparingly — meant for genuine session checkpoints, not every tool call."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "summary": {"type": "string", "description": "Short human-readable summary of where this session paused."},
            "state": {"type": "object", "description": "Optional structured state blob the engine can hand back to a future session."}
        }, "required": ["session", "summary"]}),
        Tool(name="episodic_recall", description=(
            "Recall any episodic memory stored for the CURRENT tab's domain. Returns the most recent N "
            "episodes plus the latest structured state blob. Call at the start of a session to ask "
            "'where did I leave off here?' Pure local, zero AI cost."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "domain": {"type": "string", "description": "Optional override; defaults to current tab's domain."},
            "limit": {"type": "integer", "default": 5}
        }, "required": ["session"]}),
        Tool(name="drift_heal_suggest", description=(
            "When a recorded selector breaks (drift), suggest a replacement by scanning the current DOM for elements "
            "matching the old selector's accessibility name + role + position + framework hints from the playbook. "
            "Returns {ok, candidates: [{selector, score, reason}]} ranked by likelihood. Caller decides whether to "
            "auto-apply or surface to author for review. Pair with the playbook's action_log to find what selector "
            "was last seen working on this domain."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"}, "tab": {"type": "string"},
            "old_selector": {"type": "string", "description": "The selector that just failed."},
            "descriptor": {"type": "string", "description": "Plain-English descriptor of what the element should do (e.g. 'post tweet button', 'price input for USD'). Used to match candidates."}
        }, "required": ["session", "old_selector", "descriptor"]}),
        Tool(name="reddit_check_shadowban", description=(
            "Detect whether a Reddit account is shadowbanned. Fetches the account's public profile JSON "
            "(/user/<username>/about.json + /user/<username>/comments.json) from an anonymous perspective — "
            "what the rest of Reddit sees. Compares against optional `expected_min_comments` to flag accounts "
            "that show 0 anonymous comments but are known to have posted. Also reads account age, total karma, "
            "and is_suspended fields. No Chrome session needed — pure HTTP. "
            "Returns {username, shadowbanned: bool, suspended: bool, anon_comments_count, link_karma, comment_karma, "
            "account_age_days, signals: [...]}."
        ), inputSchema={"type": "object", "properties": {
            "username": {"type": "string", "description": "Reddit username (without u/ prefix)"},
            "expected_min_comments": {"type": "integer", "default": 0, "description": "If you know the account has posted at least N comments, flag shadowban when anon view shows fewer. Default 0 (only catches full-profile-hidden shadowbans)."}
        }, "required": ["username"]}),
        Tool(name="reddit_submit_comment", description=(
            "End-to-end orchestration: navigate to a Reddit post URL, open the comment composer (handles Lexical mount timing), inject markdown text, verify the rendered text matches what we intended, click submit, and verify the comment landed. "
            "Uses lexical_set_text internally for the editor portion. Returns the new comment's URL/ID if successful, or a structured failure with the trace. "
            "Assumes the user is already logged in on this session (uses Chrome's existing cookies)."
        ), inputSchema={"type": "object", "properties": {
            "session": {"type": "string"},
            "tab": {"type": "string"},
            "post_url": {"type": "string", "description": "Full URL of the Reddit post to comment on"},
            "markdown": {"type": "string", "description": "Comment body in markdown (Reddit renders shortcuts)"},
            "verify_landed": {"type": "boolean", "default": True, "description": "After submit, re-check the page for our comment (catches AutoMod silent removals)."}
        }, "required": ["session", "post_url", "markdown"]}),
        Tool(name="touch_tap", description="Fire a real touch event (CDP Input.dispatchTouchEvent — isTrusted=true touchStart + touchEnd) at an element's center. Use for mobile-first webapps that listen for touchstart/touchend instead of mouse/click — Telegram Web /a/, WhatsApp Web, Instagram Web mobile mode, anything with a custom tap detector. Enables Emulation.setTouchEmulationEnabled if not already on. Pass a CSS selector OR an @eN ref from scan_tab.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "selector": {"type": "string", "description": "CSS selector for the element to tap (or @eN ref)."}, "double": {"type": "boolean", "default": False, "description": "Fire two taps in quick succession (double-tap)."}}, "required": ["session", "selector"]}),
        Tool(name="replay_xhr", description="Replay a captured network request with optional modifications. Takes a request shape (method, url, headers, body) usually obtained from capture_network_start/stop, optionally substitutes new values, and re-fires via fetch() with credentials:'include' so cookies/auth carry. Returns full response. Use to: (a) confirm a discovered endpoint actually works, (b) re-fire idempotent actions, (c) skip the UI entirely once an endpoint is known. Companion to xhr_upload — replay_xhr is for non-file requests.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "url": {"type": "string", "description": "Full URL to fetch (absolute or relative to current page origin)"}, "method": {"type": "string", "default": "POST"}, "headers": {"type": "object", "description": "Request headers (do not include Cookie — that flows automatically via credentials:'include')"}, "body": {"description": "Request body. If object → JSON-encoded (Content-Type: application/json added if absent). If string → sent as-is. If null/omitted → no body."}, "params": {"type": "object", "description": "Optional URL query params merged into the URL"}}, "required": ["session", "url"]}),
        Tool(name="inject_on_new_document", description="Register a JS script that auto-injects on every new document in this tab — survives all navigations (CDP Page.addScriptToEvaluateOnNewDocument). Use to install persistent XHR interceptors, fingerprint probes, or instrumentation that needs to be present BEFORE page scripts run. The script runs in page context. Returns an identifier you can pass to remove_injected_script to remove it. Common pattern: install an XHR interceptor that populates window.__xhrCaptures with full headers + body for a target endpoint regex, then drive the user through actions on multiple pages and read window.__xhrCaptures via eval_js whenever needed.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "script": {"type": "string", "description": "JavaScript source to inject. Runs in page context on every navigation BEFORE page scripts. Typical usage: install Object.defineProperty/proxy hooks on window.fetch or XMLHttpRequest.prototype that capture into a window-level array."}}, "required": ["session", "script"]}),
        Tool(name="remove_injected_script", description="Remove a previously injected persistent script by its identifier (returned from inject_on_new_document). Future navigations on this tab will no longer auto-install it.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "identifier": {"type": "string", "description": "Identifier returned by inject_on_new_document"}}, "required": ["session", "identifier"]}),
        Tool(name="xhr_upload", description="Direct file upload via fetch()/FormData against a server endpoint. Bypasses widgets that intercept and corrupt programmatic file injection (KDP AjaxInput, custom AJAX uploaders that clear input.files after onchange). Uses the page's cookies/CSRF/session via credentials:'include' — appears identical to the page's own upload request. Pair with capture_network_start/stop to discover the URL and field names by watching one manual upload. Max 25MB combined.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "url": {"type": "string", "description": "Upload endpoint URL (absolute or relative to current page origin). Discover via capture_network_start during a real upload."}, "files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute local file path"}, "field": {"type": "string", "description": "FormData field name (e.g. 'file', 'cover_image', 'manuscript'). Watch a real upload to identify."}}, "required": ["path", "field"]}, "description": "Files to upload, each with its FormData field name."}, "fields": {"type": "object", "description": "Additional FormData fields (CSRF tokens, book IDs, hidden params). Often required — capture a real upload to identify."}, "method": {"type": "string", "default": "POST"}, "headers": {"type": "object", "description": "Extra headers (X-CSRF-Token, etc). Cookies are automatic via credentials:'include'."}}, "required": ["session", "url", "files"]}),
        Tool(name="key_press", description="Press a single named key via CDP Input.dispatchKeyEvent. Use for Enter, Tab, Escape, ArrowUp/Down/Left/Right, Backspace, Space. For navigating React dropdowns/menus (Radix, Headless UI) keyboard-style: focus the trigger, ArrowDown to highlight, Enter to select.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string"}, "key": {"type": "string", "description": "Key name: Enter, Tab, Escape, ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Backspace, Space, PageDown, PageUp, Home, End"}, "modifiers": {"type": "array", "items": {"type": "string", "enum": ["Alt", "Control", "Meta", "Shift"]}, "description": "Optional modifier keys held during the press"}}, "required": ["session", "key"]}),
        Tool(name="x_create_tweet", description="Post a tweet on X without typing into the DOM. Computes x-client-transaction-id from the page (reverses X's body-signed anti-replay header), refreshes ct0 from cookie, fires CreateTweet GraphQL POST from the tab context (cookies auto-attach). Zero manual seed needed — kills the seed-and-replay dance entirely. Use this instead of draftjs_set_text+click for X posting. Requires the X tab to be open and logged in.", inputSchema={"type": "object", "properties": {"session": {"type": "string"}, "tab": {"type": "string", "description": "Tab matching x.com (defaults to first x.com tab)"}, "text": {"type": "string", "description": "Tweet body"}, "query_id": {"type": "string", "description": "X's CreateTweet GraphQL queryId. Optional — if omitted, scraped from the page. Pass explicitly when scrape fails (X bundles changed)."}, "reply_to_tweet_id": {"type": "string", "description": "Optional — if set, posts as a reply to this tweet id."}}, "required": ["session", "text"]}),
    ]

def resolve_session(session_str: str) -> int:
    """Returns port number for a session name or direct port string."""
    sessions = load_sessions()
    if session_str in sessions:
        return sessions[session_str]["port"]
    try:
        return int(session_str)
    except ValueError:
        raise ValueError(f"Unknown session '{session_str}'. Available: {list(sessions.keys())}")

_STATUS_FREE = {"chrome_status", "list_sessions", "list_running_chrome", "get_playbook", "note", "save_playbook", "find_tab_by_selector", "start_recording", "end_recording", "list_recipes", "replay_recipe", "confirm_login", "detect_blocker", "auth_totp", "pause_for_human", "export_profile", "import_profile", "list_threads", "install_thread", "export_thread", "reddit_check_shadowban"}

# Per-session login confirmation: any interaction tool requires the session to be in this set.
# Cleared when launch_session opens a fresh window for that session.
_LOGIN_CONFIRMED: set = set()
_REQUIRES_LOGIN = {"scan_tab", "click", "fill", "eval_js", "read_tab", "screenshot", "wait_for", "scroll_tab", "upload_file", "key_type", "key_press", "react_force_change", "react_inspect_store", "redux_dispatch", "aui_dispatch", "backbone_inspect", "xhr_upload", "touch_tap", "replay_xhr", "lexical_set_text", "draftjs_set_text", "reddit_submit_comment", "inject_on_new_document", "remove_injected_script", "vision_check", "click_at_coords", "enable_stealth", "solve_captcha", "drift_heal_suggest", "visual_diff_preflight", "subscribe_to_websocket", "poll_websocket_messages", "episodic_remember", "episodic_recall", "swarm_run", "react_invoke_handler", "x_create_tweet"}
# check_thread_updates + show_recent_auto_heals don't need a session — they're admin/info tools


# ── Background auto-update timer ────────────────────────────────────────
# Fires the auto-update sweep once on engine startup (after a short grace
# delay so we don't slow tool registration) and then every 6h. All errors
# swallowed — telemetry/update failures must never break a user's flow.
def _start_auto_update_timer():
    if not AUTO_UPDATE_ENABLED:
        return
    import threading
    def _tick():
        try:
            _auto_update_threads_once()
        except Exception:
            pass
        # Reschedule
        t = threading.Timer(_AUTO_UPDATE_INTERVAL_S, _tick)
        t.daemon = True
        t.start()
    # First fire 30 seconds after import — gives Chrome session + Anthropic API a head start
    t0 = threading.Timer(30.0, _tick)
    t0.daemon = True
    t0.start()
_start_auto_update_timer()
# Detection/probe/idle tools don't require login — they're discovery-tier
_STATUS_FREE_EXTRA = {"detect_anti_bot", "framework_detect", "wait_for_idle", "seed_from_tab"}

# Startup check fires every new "session" (10 min of no Chrome MCP activity = new session)
_STARTUP_STATE = Path.home() / ".webloom" / "startup-check.json"
_STARTUP_RESET_SECONDS = 600

def _startup_was_done_recently() -> bool:
    try:
        if not _STARTUP_STATE.exists():
            return False
        import time
        data = json.loads(_STARTUP_STATE.read_text())
        last = data.get("last_check", 0)
        return (time.time() - last) < _STARTUP_RESET_SECONDS
    except Exception:
        return False

def _mark_startup_done():
    try:
        import time
        _STARTUP_STATE.parent.mkdir(parents=True, exist_ok=True)
        _STARTUP_STATE.write_text(json.dumps({"last_check": time.time()}))
    except Exception:
        pass


async def _handle_recipe_tool(name: str, arguments: dict):
    """Handle the 4 recipe-management tools. Returns list[TextContent] or None if not a recipe tool."""
    global _replaying
    if name == "start_recording":
        rname = arguments.get("name", "").strip()
        if not rname:
            return [TextContent(type="text", text="Error: recipe name required.")]
        res = recording.start(rname, arguments.get("goal", ""), arguments.get("domain", ""))
        if not res.get("ok"):
            return [TextContent(type="text", text=f"Failed: {res.get('error')}")]
        return [TextContent(type="text", text=f"📹 Recording started: **{rname}**\nGoal: {arguments.get('goal','(none)')}\nEvery click/fill/navigate/wait_for/eval_js will be logged until you call end_recording.")]

    if name == "end_recording":
        outcome = arguments.get("outcome", "success")
        params = arguments.get("parameters", [])
        res = recording.end(outcome=outcome, parameters=params)
        if not res.get("ok"):
            return [TextContent(type="text", text=f"Failed: {res.get('error')}")]
        if outcome != "success":
            return [TextContent(type="text", text=f"Recording discarded (outcome={outcome}).")]
        return [TextContent(type="text", text=f"✅ Recipe saved: **{res['recipe']}**\nActions captured: {res['action_count']}\nReplay with: replay_recipe(name='{res['recipe']}')")]

    if name == "list_recipes":
        recipes = recording.list_recipes()
        if not recipes:
            return [TextContent(type="text", text="No recipes saved yet. Use start_recording to capture one.")]
        lines = ["## Saved recipes\n"]
        for r in recipes:
            params = f" · params: {', '.join(r['parameters'])}" if r["parameters"] else ""
            lines.append(f"• **{r['name']}** ({r['actions']} actions{params})\n  Goal: {r['goal'] or '(no goal)'}")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "replay_recipe":
        rname = arguments.get("name", "").strip()
        recipe = recording.load_recipe(rname)
        if not recipe:
            available = [r["name"] for r in recording.list_recipes()]
            return [TextContent(type="text", text=f"Recipe '{rname}' not found. Available: {available}")]
        params = arguments.get("parameters", {}) or {}
        session_override = arguments.get("session")
        tab_override = arguments.get("tab")
        log = [f"## Replay: **{rname}** ({len(recipe['actions'])} actions)\n"]
        _replaying = True
        try:
            for i, action in enumerate(recipe["actions"], 1):
                tool = action["tool"]
                args = recording.substitute_params(action.get("args", {}), params)
                if session_override:
                    args["session"] = session_override
                if tab_override and "tab" in action.get("args", {}):
                    args["tab"] = tab_override
                log.append(f"**[{i}/{len(recipe['actions'])}] {tool}**  args={json.dumps({k:v for k,v in args.items() if k not in ('code',)}, default=str)[:200]}")
                try:
                    result = await _execute_tool_action(tool, args)
                    # Brief result summary
                    res_text = ""
                    if result and isinstance(result, list):
                        for item in result:
                            if hasattr(item, "text"):
                                res_text = item.text[:160]
                                break
                    log.append(f"   → {res_text or 'ok'}")
                except Exception as e:
                    log.append(f"   ❌ ERROR: {e}")
                    log.append(f"\n**Replay aborted at step {i}.** Fix the issue and re-run, or record a new version.")
                    return [TextContent(type="text", text="\n".join(log))]
            log.append(f"\n✅ Replay complete — all {len(recipe['actions'])} actions ran successfully.")
            return [TextContent(type="text", text="\n".join(log))]
        finally:
            _replaying = False

    return None


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # Recipe-management tools handled before everything else; not logged themselves
    recipe_result = await _handle_recipe_tool(name, arguments)
    if recipe_result is not None:
        return recipe_result

    # Execute the real action and capture the result
    result = await _execute_tool_action(name, arguments)

    # Auto-log to current recipe if recording (and not replaying)
    if not _replaying and recording.is_recording() and name in recording.ACTION_TOOLS:
        summary = "ok"
        try:
            if result and isinstance(result, list):
                for item in result:
                    if hasattr(item, "text"):
                        summary = item.text[:120]
                        break
        except Exception:
            pass
        recording.log_action(name, arguments, result_summary=summary)
    return result


async def _execute_tool_action(name: str, arguments: dict):
    # Skip the session-check gate if the tool already targets a live session.
    target_session = arguments.get("session", "") if isinstance(arguments, dict) else ""
    _live_target = False
    if target_session:
        try:
            _sessions_cfg = load_sessions()
            if target_session in _sessions_cfg:
                _running = discover_running_chrome_sessions()
                _live_ports = {r["port"] for r in _running}
                if _sessions_cfg[target_session]["port"] in _live_ports:
                    _live_target = True
        except Exception:
            pass

    if not _startup_was_done_recently() and name not in _STATUS_FREE and not _live_target:
        _mark_startup_done()
        sessions = load_sessions()
        running = discover_running_chrome_sessions()
        running_ports = {r["port"]: r for r in running}
        live, offline = [], []
        lines = ["## Chrome MCP — Session Check\n"]
        for sname, cfg in sessions.items():
            r = running_ports.get(cfg["port"])
            if r:
                tabs = " | ".join(r["tabs"][:3]) or "(no tabs)"
                lines.append(f"  ✅ **{sname}** (port {cfg['port']}) — {r['tab_count']} tab(s): {tabs}")
                live.append(sname)
            else:
                lines.append(f"  ❌ **{sname}** (port {cfg['port']}) — offline")
                offline.append(sname)
        lines.append("")
        if live:
            live_list = ", ".join(f"`{s}`" for s in live)
            offline_list = ", ".join(f"`{s}`" for s in offline)
            lines.append(f"**Live:** {live_list}   **Offline:** {offline_list if offline else 'none'}")
        else:
            lines.append(f"**No sessions running.** All offline: {', '.join(f'`{s}`' for s in offline)}")
        lines.append("")
        session_options = []
        for sname in sessions:
            icon = "🟢" if sname in live else "⚫"
            session_options.append(f"{icon} {sname}")
        options_str = ", ".join(f'"{o}"' for o in session_options)
        lines.append("⏸️ **STOP — call `AskUserQuestion` RIGHT NOW** with:")
        lines.append(f'  question: "Which Chrome session should I open?"')
        lines.append(f'  header: "Chrome Session"')
        lines.append(f'  options: [{options_str}]')
        lines.append("")
        lines.append("Do NOT proceed until the user answers. After they pick, call `launch_session(session=<choice>, confirmed_by_user=true)`.")
        return [TextContent(type="text", text="\n".join(lines))]

    sessions = load_sessions()

    # Login gate: interaction tools require the session to be login-confirmed.
    if name in _REQUIRES_LOGIN:
        target_session = arguments.get("session", "")
        if target_session and target_session not in _LOGIN_CONFIRMED:
            return [TextContent(type="text", text=(
                f"⏸️ **LOGIN CHECK REQUIRED for '{target_session}'** — do not proceed.\n\n"
                f"Call `AskUserQuestion` RIGHT NOW with:\n"
                f'  question: "Are you logged in to the site in the slot-{target_session} Chrome window?"\n'
                f'  header: "Login Check"\n'
                f'  options: ["Yes, logged in", "Not yet — let me log in"]\n\n'
                f"After the user answers:\n"
                f"  • If \"Yes\" → call `confirm_login(session=\"{target_session}\", logged_in=true)` then retry the original call.\n"
                f"  • If \"Not yet\" → call `confirm_login(session=\"{target_session}\", logged_in=false)` and wait.\n"
                f"Do NOT skip this — interaction tools (scan_tab, click, fill, eval_js, read_tab, screenshot, wait_for, scroll_tab) are blocked until login is confirmed."
            ))]

    if name == "list_sessions":
        lines = []
        any_live = False
        for sname, cfg in sessions.items():
            port = cfg["port"]
            tabs = get_tabs(port)
            if tabs:
                status = f"✅ LIVE — {len(tabs)} tab{'s' if len(tabs) != 1 else ''}"
                any_live = True
            else:
                status = "❌ offline"
            lines.append(f"**{sname}** (port {port}): {status}\n  {cfg['description']}")
        if not any_live:
            lines.append("\n---\nNo Chrome sessions running. Use `launch_session` to start one — e.g. launch_session(session=\"main\") to open your real Chrome with saved logins, or launch_session(session=\"farm\") for a fresh game/farm browser.")
        return [TextContent(type="text", text="\n\n".join(lines))]

    if name == "confirm_login":
        sname = arguments.get("session", "").strip()
        logged_in = arguments.get("logged_in", False)
        if not sname:
            return [TextContent(type="text", text="Error: session name required.")]
        if logged_in:
            _LOGIN_CONFIRMED.add(sname)
            return [TextContent(type="text", text=f"✅ Login confirmed for '{sname}'. You may now use scan_tab, click, fill, etc.")]
        else:
            _LOGIN_CONFIRMED.discard(sname)
            return [TextContent(type="text", text=f"⏳ Waiting for login on '{sname}'. Tell the user: \"Let me know when you've finished logging in, then I'll continue.\" Do NOT proceed with any interaction tools until they confirm and you call `confirm_login` again with logged_in=true.")]

    if name == "launch_session":
        sname = arguments.get("session", "")
        force_new = arguments.get("force_new", False)
        confirmed_by_user = arguments.get("confirmed_by_user", False)

        # Always discover what's already running first
        running = discover_running_chrome_sessions()

        # Gate: if user hasn't confirmed a session choice, stop and trigger a VS Code popup.
        if not confirmed_by_user:
            sessions_cfg = load_sessions()
            running_ports = {r["port"] for r in running}
            session_options = []
            for s, cfg in sessions_cfg.items():
                status = "🟢" if cfg["port"] in running_ports else "⚫"
                session_options.append(f"{status} {s}")
            options_str = ", ".join(f'"{o}"' for o in session_options)
            return [TextContent(type="text", text=(
                f"CHROME SESSION REQUIRED — do not proceed.\n\n"
                f"Call the `AskUserQuestion` tool RIGHT NOW with this exact question:\n"
                f"  question: \"Which Chrome session should I open?\"\n"
                f"  header: \"Chrome Session\"\n"
                f"  options: [{options_str}]\n\n"
                f"Wait for the user's answer, then call `launch_session` again with the chosen session name and `confirmed_by_user=true`.\n"
                f"Do NOT call any other Chrome tool before the user answers."
            ))]

        # If no session specified or not force_new — show what's available
        if running and not force_new:
            lines = ["**Running Chrome debug instances — pick one to connect:**\n"]
            for r in running:
                tab_list = ", ".join(r["tabs"]) if r["tabs"] else "(no tabs)"
                lines.append(f"  • **{r['name']}** (port {r['port']}) — {r['tab_count']} tab(s): {tab_list}")
            if sname and sname in sessions:
                cfg = sessions[sname]
                port = cfg["port"]
                already = next((r for r in running if r["port"] == port), None)
                if already:
                    lines.append(f"\n✅ **'{sname}' is already live on port {port}** — connected. Use scan_tab or list_tabs to continue.")
                    return [TextContent(type="text", text="\n".join(lines))]
            lines.append(f"\nTo connect to one of these, call `list_tabs(session='<name>')` or `scan_tab(session='<name>')`.")
            lines.append("To open a **new** Chrome window anyway, call `launch_session(session='<name>', force_new=true)`.")
            return [TextContent(type="text", text="\n".join(lines))]

        if not running and not sname:
            return [TextContent(type="text", text="No Chrome debug instances running. Call launch_session(session='main') to start one.")]

        if not sname or sname not in sessions:
            available = list(sessions.keys())
            return [TextContent(type="text", text=f"Unknown session '{sname}'. Available: {available}")]

        cfg = sessions[sname]
        port = cfg["port"]
        user_data_dir = cfg["user_data_dir"]

        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        args = [
            CHROME_EXE,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process,Translate",
            "--disable-infobars",
        ]
        if arguments.get("url"):
            args.append(arguments["url"])

        subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        await asyncio.sleep(2)

        # New session window — login state is unknown; clear any prior confirmation.
        _LOGIN_CONFIRMED.discard(sname)

        url_opened = arguments.get("url") or "(no URL)"
        login_instruction = (
            f"\n\n⏸️ **STOP — call `AskUserQuestion` RIGHT NOW** with:\n"
            f'  question: "Are you logged in to {url_opened} in the slot-{sname} window?"\n'
            f'  header: "Login Check"\n'
            f'  options: ["Yes, logged in", "Not yet — let me log in"]\n\n'
            f"After the user answers:\n"
            f"  • If \"Yes\" → call `confirm_login(session=\"{sname}\", logged_in=true)` and then proceed.\n"
            f"  • If \"Not yet\" → call `confirm_login(session=\"{sname}\", logged_in=false)` and wait for the user to say they're done.\n"
            f"Do NOT call scan_tab, click, fill, eval_js, or any other interaction tool before login is confirmed."
        )

        tabs = get_tabs(port)
        if tabs:
            tab_titles = [t.get("title", "(loading)") for t in tabs[:3]]
            return [TextContent(type="text", text=f"✅ Launched '{sname}' on port {port}.\nTabs: {', '.join(tab_titles)}" + login_instruction)]
        return [TextContent(type="text", text=f"Chrome launched for '{sname}' — still starting up." + login_instruction)]

    # Session-free tools — handle before session resolution
    if name == "chrome_status":
        sessions = load_sessions()
        running = discover_running_chrome_sessions()
        running_ports = {r["port"]: r for r in running}
        live_sessions = []
        offline_sessions = []
        lines = ["## Chrome MCP Status\n"]
        lines.append("**Configured sessions:**")
        for sname, cfg in sessions.items():
            port_n = cfg["port"]
            r = running_ports.get(port_n)
            if r:
                tab_titles = r["tabs"][:3]
                tab_str = " | ".join(tab_titles) if tab_titles else "(no tabs)"
                lines.append(f"  ✅ **{sname}** (port {port_n}) — {r['tab_count']} tab(s): {tab_str}")
                live_sessions.append(sname)
            else:
                lines.append(f"  ❌ **{sname}** (port {port_n}) — offline")
                offline_sessions.append(sname)
        unknown = [r for r in running if r["name"].startswith("unknown:")]
        if unknown:
            lines.append("\n**Unknown Chrome instances (no session config):**")
            for r in unknown:
                lines.append(f"  ⚠️  port {r['port']} — {r['tab_count']} tab(s): {', '.join(r['tabs'][:2])}")
        lines.append("")
        if live_sessions:
            first = live_sessions[0]
            lines.append(f"**Ready:** `{first}` is live — use `scan_tab(session=\"{first}\")` or `list_tabs(session=\"{first}\")`")
        else:
            first_offline = offline_sessions[0] if offline_sessions else "main"
            lines.append(f"**No sessions running** — launch one with `launch_session(session=\"{first_offline}\")`")
        pb = load_playbook()
        if pb:
            lines.append(f"\n**Playbook:** {len(pb)} domain(s) learned — {', '.join(list(pb.keys())[:5])}")
        else:
            lines.append("\n**Playbook:** empty — run `scan_tab` on any site to start building it")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "note":
        domain = arguments["domain"].replace("www.", "")
        text = arguments["text"]
        playbook = _load_live_playbook_raw()  # write to live only, not merged
        if domain not in playbook:
            playbook[domain] = {}
        notes = playbook[domain].get("notes", [])
        notes.append(text)
        playbook[domain]["notes"] = notes
        save_playbook_data(playbook)
        return [TextContent(type="text", text=f"Note saved for {domain}: \"{text}\"")]

    if name == "list_running_chrome":
        running = discover_running_chrome_sessions()
        if not running:
            return [TextContent(type="text", text="No Chrome instances with debug ports found.\nLaunch one with: launch_session(session='main', force_new=true)")]
        lines = [f"**{len(running)} Chrome debug instance(s) running:**\n"]
        for r in running:
            tabs = ", ".join(r["tabs"]) if r["tabs"] else "(no tabs)"
            lines.append(f"  • **{r['name']}** — port {r['port']} — {r['tab_count']} tab(s)\n    {tabs}")
        lines.append("\nConnect with: `scan_tab(session='<name>')` or `list_tabs(session='<name>')`")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "get_playbook":
        playbook = load_playbook()
        domain = arguments.get("domain", "")
        if not domain:
            if not playbook:
                return [TextContent(type="text", text="Playbook is empty — run scan_tab on any site to start building it.")]
            domains = list(playbook.keys())
            return [TextContent(type="text", text=f"**Known domains ({len(domains)}):**\n" + "\n".join(f"- {d}" for d in domains))]
        if domain not in playbook:
            return [TextContent(type="text", text=f"No playbook entry for {domain} yet. Run scan_tab on that site first.")]
        return [TextContent(type="text", text=f"## Playbook: {domain}\n\n```json\n{json.dumps(playbook[domain], indent=2)}\n```")]

    if name == "save_playbook":
        domain = arguments["domain"].replace("www.", "")
        key = arguments["key"]
        value = arguments["value"]
        # Defensive: if value comes in as a string that LOOKS like JSON
        # (list/object), parse it. Tool-call layers sometimes JSON-encode
        # complex values mid-flight; we want to store the real structure.
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("[", "{")):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, (list, dict)):
                        value = parsed
                except Exception:
                    pass
        playbook = _load_live_playbook_raw()  # write to live only, not merged
        if domain not in playbook:
            playbook[domain] = {}
        playbook[domain][key] = value
        save_playbook_data(playbook)
        return [TextContent(type="text", text=f"Saved `{key}` for {domain} to playbook (type={type(value).__name__}).")]

    if name == "list_threads":
        threads = load_threads()
        if not threads:
            return [TextContent(type="text", text=(
                "No Threads installed yet.\n\n"
                f"  Bundled  : {PROJECT_THREADS_DIR}\n"
                f"  User dir : {THREADS_DIR}\n\n"
                "Install one with `install_thread(path=...)`, or export your live "
                "playbook learning as a Thread via `export_thread(domain=...)`."
            ))]
        lines = [f"## {len(threads)} Thread(s) installed\n"]
        for dom, t in threads.items():
            v = t.get("version", "?")
            framework = t.get("framework", "?")
            author = t.get("author", "?")
            keys = [k for k in t.keys() if k not in ("domain", "name", "version", "author", "license")][:10]
            lines.append(f"**{dom}** v{v}")
            lines.append(f"  framework: {framework}  ·  author: {author}")
            lines.append(f"  contributes: {', '.join(keys)}")
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "install_thread":
        src = Path(arguments["path"])
        overwrite = bool(arguments.get("overwrite", False))
        if not src.is_file():
            return [TextContent(type="text", text=f"install_thread: file not found: {src}")]
        data = _load_thread_file(src)
        if not data:
            return [TextContent(type="text", text=f"install_thread: invalid Thread (missing 'domain' or not JSON): {src}")]
        domain = data["domain"]
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = THREADS_DIR / f"{domain}.thread.json"
        if dest.exists() and not overwrite:
            return [TextContent(type="text", text=f"install_thread: {dest} already exists. Pass overwrite=true to replace.")]
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return [TextContent(type="text", text=(
            f"Installed Thread for **{domain}** → {dest}\n"
            f"  framework: {data.get('framework','?')}  ·  version: {data.get('version','?')}\n"
            f"  WebLoom will auto-consult this Thread for {domain} on next read."
        ))]

    if name == "export_thread":
        domain = arguments["domain"].replace("www.", "")
        out_path = arguments.get("out_path")
        import time as _t
        meta = {
            "name": arguments.get("name", domain),
            "version": arguments.get("version", "1.0.0"),
            "author": arguments.get("author", ""),
            "license": arguments.get("license", "proprietary"),
            "created_at": int(_t.time()),
        }
        # Use live playbook only (no Thread merge — we don't want to export Thread A's data as Thread B)
        live = _load_live_playbook_raw()
        entry = live.get(domain)
        if not entry:
            return [TextContent(type="text", text=f"export_thread: no live playbook entry for {domain}. Run scan/click on it first to accumulate knowledge.")]
        thread = {"domain": domain, **{k: v for k, v in meta.items() if v}, **entry}
        if not out_path:
            THREADS_DIR.mkdir(parents=True, exist_ok=True)
            out_path = str(THREADS_DIR / f"{domain}.thread.json")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(thread, indent=2), encoding="utf-8")
        return [TextContent(type="text", text=(
            f"Exported live playbook for **{domain}** → {out_path}\n"
            f"  {len(thread)-1} keys captured  ·  ready to share, install on another machine, or sell."
        ))]

    if name == "reddit_check_shadowban":
        username = arguments["username"].lstrip("u/").lstrip("/").strip()
        min_expected = int(arguments.get("expected_min_comments", 0) or 0)
        if not username:
            return [TextContent(type="text", text="reddit_check_shadowban: username required")]

        def _fetch_json(url: str, timeout: float = 8.0) -> tuple[int, dict | None]:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 WebLoom/0.2 (compatible; checker)"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
            except Exception:
                return 0, None

        about_status, about = _fetch_json(f"https://www.reddit.com/user/{username}/about.json")
        comments_status, comments = _fetch_json(f"https://www.reddit.com/user/{username}/comments.json?limit=25")
        submitted_status, submitted = _fetch_json(f"https://www.reddit.com/user/{username}/submitted.json?limit=25")

        signals: list[str] = []
        shadowbanned = False
        suspended = False
        link_karma = 0
        comment_karma = 0
        account_age_days = 0
        anon_comment_count = 0
        anon_submitted_count = 0

        # 404 on about.json is the classic "user does not exist to anonymous viewers" signal — strong shadowban marker
        if about_status == 404:
            signals.append("about.json returned 404 (profile not visible to anonymous)")
            shadowbanned = True

        # 403 on about.json typically means suspended (different from shadowban)
        if about_status == 403:
            signals.append("about.json returned 403 (account suspended)")
            suspended = True

        if about and about_status == 200:
            data = about.get("data", {}) or {}
            link_karma = data.get("link_karma", 0)
            comment_karma = data.get("comment_karma", 0)
            is_suspended = bool(data.get("is_suspended"))
            if is_suspended:
                signals.append("about.json data.is_suspended = true")
                suspended = True
            created_utc = data.get("created_utc")
            if created_utc:
                import time as _t
                account_age_days = int((_t.time() - created_utc) / 86400)

        if comments and comments_status == 200:
            children = ((comments.get("data") or {}).get("children") or [])
            anon_comment_count = len(children)
        elif comments_status == 404:
            signals.append("comments.json returned 404")
            shadowbanned = True

        if submitted and submitted_status == 200:
            anon_submitted_count = len(((submitted.get("data") or {}).get("children") or []))

        # Heuristic: account has karma but zero anonymous-visible comments/posts → likely shadowbanned
        if (not shadowbanned and not suspended
                and comment_karma > 5
                and anon_comment_count == 0
                and anon_submitted_count == 0):
            signals.append(f"account has comment_karma={comment_karma} but anonymous view returns 0 comments + 0 submissions")
            shadowbanned = True

        if min_expected > 0 and anon_comment_count < min_expected:
            signals.append(f"anon_comment_count={anon_comment_count} < expected_min_comments={min_expected}")
            shadowbanned = True

        verdict_str = "🚧 SHADOWBANNED" if shadowbanned else ("⛔ SUSPENDED" if suspended else "✅ visible")
        lines = [
            f"## Reddit check: u/{username}",
            f"**Verdict:** {verdict_str}",
            "",
            f"  account age: {account_age_days} days",
            f"  link_karma:  {link_karma}",
            f"  comment_karma: {comment_karma}",
            f"  anon-visible comments (last 25): {anon_comment_count}",
            f"  anon-visible submissions (last 25): {anon_submitted_count}",
            "",
        ]
        if signals:
            lines.append("**Signals:**")
            for s in signals:
                lines.append(f"  • {s}")
        else:
            lines.append("No shadowban / suspension signals detected.")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "pause_for_human":
        reason = arguments.get("reason", "human action required")
        instructions = arguments.get("instructions", "")
        if arguments.get("beep", True):
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
        # Record manual-touch checkpoint in the playbook. We tag the
        # MOST RECENTLY ACTIVE domain in session_state so the gap is
        # attached to the right flow. Future Thread authors see exactly
        # which step needs human help.
        try:
            most_recent_domain = None
            most_recent_ts = 0
            for d, st in _session_state.items():
                ts = float(st.get("last_ts", 0) or 0)
                if ts > most_recent_ts:
                    most_recent_ts = ts
                    most_recent_domain = d
            if most_recent_domain:
                desc = f"pause_for_human:{reason[:60]}"
                _playbook_record(
                    most_recent_domain, desc, "manual", True,
                    kind="manual_touch",
                    manual_touch_required=True,
                    manual_touch_reason=reason,
                )
        except Exception:
            pass
        msg = (
            f"⏸️ **PAUSED — human action required**\n\n"
            f"**Reason:** {reason}\n"
            + (f"**Instructions for user:** {instructions}\n\n" if instructions else "\n")
            + "Caller: ask the user via AskUserQuestion to confirm when they've completed the action, then resume the flow."
        )
        return [TextContent(type="text", text=msg)]

    # All other tools need a session
    port = resolve_session(arguments["session"])
    tabs = get_tabs(port)
    if not tabs:
        return [TextContent(type="text", text=f"Session '{arguments['session']}' is offline. Launch Chrome first:\n  .\\launch.ps1 -Session {arguments['session']}")]

    if name == "list_tabs":
        visible = real_tabs(tabs)
        lines = [f"**{i}. {t.get('title','(no title)')}**\n   {t.get('url','')}\n   id: {t['id']}" for i, t in enumerate(visible)]
        return [TextContent(type="text", text="\n\n".join(lines))]

    # Resolve tab
    tab_ref = arguments.get("tab", "")
    tab = find_tab(tabs, tab_ref)
    if not tab:
        return [TextContent(type="text", text="No tabs found in this session.")]
    ws_url = tab["webSocketDebuggerUrl"]

    # Resolve @eN references — any arg that holds a selector pointing into the
    # last AX scan gets expanded to a real CSS selector before downstream use.
    _ax_keys = ("description", "selector", "drop_target_selector", "inject_input_selector",
                "click_first_selector", "from_selector", "target_selector")
    for _k in _ax_keys:
        if _k in arguments and isinstance(arguments[_k], str) and arguments[_k].startswith("@e"):
            arguments[_k] = await resolve_ax_ref(ws_url, arguments[_k])

    if name == "read_tab":
        result = await eval_in_tab(ws_url, READ_JS)
        try:
            data = json.loads(result.get("result", {}).get("value", "{}"))
            out = f"**{data.get('title')}**\n{data.get('url')}\n\n{data.get('text','')}"
            if data.get("inputs"):
                out += f"\n\n**Form fields:**\n{data['inputs']}"
            return [TextContent(type="text", text=out)]
        except Exception as e:
            return [TextContent(type="text", text=f"Error reading tab: {e}")]

    if name == "screenshot":
        data = await screenshot_tab(ws_url)
        # Record as visual checkpoint in current flow — captures that a screenshot
        # was used here, so Threads can carry "verify visually at this step" cues.
        if data:
            try:
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                from urllib.parse import urlparse
                path = urlparse(tab_url_r.get("result", {}).get("value", "")).path or "/"
                if tab_dom:
                    _playbook_record(tab_dom, f"screenshot:{path}", "screenshot", True,
                                     kind="checkpoint")
            except Exception:
                pass
            return [ImageContent(type="image", data=data, mimeType="image/jpeg")]
        return [TextContent(type="text", text="Screenshot failed.")]

    if name == "click":
        desc = arguments["description"]
        allow_real_cursor = bool(arguments.get("allow_real_cursor", False))
        debug = bool(arguments.get("debug", False))
        timeout_s = float(arguments.get("timeout_seconds", 5))
        tab_domain = domain_from_url(tab.get("url", ""))
        trace: list[str] = []

        def _log(msg):
            trace.append(msg)

        def _result(strategy_used: str, success: bool, msg: str):
            _playbook_record(tab_domain, desc, strategy_used, success)
            text = msg
            if debug:
                text += "\n\n--- TRACE ---\n" + "\n".join(trace)
            return [TextContent(type="text", text=text)]

        # ── Stage 1: playbook lookup. If we have history, prefer the known-good strategy.
        pref = _playbook_get_strategy(tab_domain, desc)
        _log(f"playbook for '{desc}' @ {tab_domain}: {pref}")

        # Domain-wide default strategy (set when a site consistently needs the same path).
        if not pref:
            pb_dom = load_playbook().get(tab_domain or "", {}) if tab_domain else {}
            ds = pb_dom.get("default_strategy")
            if ds:
                pref = {"strategy": ds, "success_rate": 1.0, "successes": 1, "failures": 0, "last_at": None, "source": "domain_default"}
                _log(f"playbook domain default for {tab_domain}: '{ds}'")

        # ── Stage 2: actionability + CDP click (primary path — Playwright-style).
        async def _try_cdp() -> tuple[bool, str]:
            probe = await wait_actionable(ws_url, desc, timeout_s=timeout_s)
            _log(f"actionability probe: {json.dumps({k: probe.get(k) for k in ('found','actionable','visible','inViewport','notDisabled','hitsTarget','animating','tag','id','intercepting')})}")
            if not probe.get("found"):
                return False, "element not found"
            if probe.get("intercepting"):
                interc = probe["intercepting"]
                _log(f"⚠️  hit-test intercepted by: <{interc.get('tag')}> id='{interc.get('id')}' class='{interc.get('cls')}' — click at these coords will not reach the target")
            if not probe.get("actionable"):
                _log("element not actionable in time — trying anyway")
            cx, cy = probe.get("cx"), probe.get("cy")
            if cx is None or cy is None:
                return False, "no coords"
            snap_before = await snapshot_for_verify(ws_url)
            await cdp_real_click(ws_url, cx, cy)
            await asyncio.sleep(0.4)
            snap_after = await snapshot_for_verify(ws_url)
            changed = snap_after != snap_before
            _log(f"cdp click @ ({cx:.0f},{cy:.0f}) → verifier changed={changed}")
            if changed:
                return True, f"clicked (actionability + CDP) '{desc}' at {cx:.0f},{cy:.0f}"
            return False, "CDP click had no visible effect"

        # ── Stage 3: JS dispatch fallback (pointerdown+mousedown+click sequence on leaf).
        async def _try_js() -> tuple[bool, str]:
            snap_before = await snapshot_for_verify(ws_url)
            js = CLICK_JS.replace("DESCRIPTION", json.dumps(desc))
            r = await eval_in_tab(ws_url, js)
            val = r.get("result", {}).get("value", "no result")
            _log(f"js dispatch result: {val[:160]}")
            if "not found" in val:
                return False, "js dispatch: not found"
            await asyncio.sleep(0.4)
            snap_after = await snapshot_for_verify(ws_url)
            changed = snap_after != snap_before
            _log(f"js verifier changed={changed}")
            if changed:
                return True, val
            return False, "JS dispatch had no visible effect"

        # ── Stage 4: vision grounding.
        async def _try_vision() -> tuple[bool, str]:
            try:
                v = await vision_ground(ws_url, desc)
            except Exception as e:
                _log(f"vision threw: {e}")
                return False, "vision error"
            _log(f"vision coords: {v}")
            if not v:
                return False, "vision returned no coords"
            snap_before = await snapshot_for_verify(ws_url)
            await cdp_real_click(ws_url, v[0], v[1])
            await asyncio.sleep(0.4)
            snap_after = await snapshot_for_verify(ws_url)
            changed = snap_after != snap_before
            _log(f"vision verifier changed={changed}")
            if changed:
                return True, f"clicked (vision/{VISION_BACKEND}) '{desc}' at {v[0]:.0f},{v[1]:.0f}"
            return False, "vision click had no visible effect"

        # NO LAYER 3: WebLoom never moves the OS cursor. Per Mariano (2026-05-16),
        # `allow_real_cursor` is a no-op kept only for backward compat — pyautogui
        # / real_cursor_click are not reachable from the click tool. If DOM/CDP/
        # vision all fail, return a clean "not interactable via DOM" error and let
        # the caller decide whether to hand off to the user.

        # ── Stage 0: AUI declarative fire ────────────────────────────────────────
        # Amazon AUI buttons (KDP, Vendor Central, A+ Content, ads console) wrap a
        # `<button>` in `<span class="a-button a-declarative" data-action="...">`.
        # A synthetic click on the inner button visibly closes a modal but DOES NOT
        # commit form state — because AUI's state machine only commits when its
        # declarative event handler runs via `A.declarative.fire(action, host, event)`.
        # Detect AUI pattern + fire the declarative event. Returns hit:false when
        # not AUI so falls through to CDP/JS for non-Amazon sites.
        async def _try_aui_dispatch() -> tuple[bool, str]:
            snap_before = await snapshot_for_verify(ws_url)
            aui_js = f"""(function() {{
                const target_desc = {json.dumps(desc.lower())};
                // Find any AUI-wrapped element matching the descriptor
                const all = document.querySelectorAll('button, [role=button], input[type=submit], a, .a-button-text, .a-button-input');
                let best = null;
                for (const el of all) {{
                    if (el.offsetParent === null) continue;
                    const t = ((el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase());
                    if (!t || (t !== target_desc && !t.includes(target_desc))) continue;
                    const auiHost = el.closest('.a-declarative[data-action], [data-action][data-component-type]');
                    if (auiHost) {{ best = {{el, host: auiHost}}; break; }}
                }}
                if (!best) return JSON.stringify({{hit: false, reason: 'no AUI ancestor with data-action found'}});
                const action = best.host.getAttribute('data-action');
                if (!window.A || !window.A.declarative || typeof window.A.declarative.fire !== 'function') {{
                    return JSON.stringify({{hit: false, reason: 'window.A.declarative.fire not available'}});
                }}
                try {{
                    // Build a synthetic event AUI's handlers expect
                    const evt = new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}});
                    window.A.declarative.fire(action, best.host, evt);
                    return JSON.stringify({{hit: true, action, tag: best.el.tagName, text: (best.el.textContent || '').trim().slice(0, 50)}});
                }} catch(e) {{
                    return JSON.stringify({{hit: false, reason: 'AUI fire threw: ' + e.message}});
                }}
            }})()"""
            r = await eval_in_tab(ws_url, aui_js)
            val = r.get("result", {}).get("value", "{}")
            try:
                info = json.loads(val) if val else {}
            except Exception:
                info = {}
            if not info.get("hit"):
                return False, f"not AUI ({info.get('reason', '?')})"
            await asyncio.sleep(0.6)  # AUI commits are slightly slower than naked clicks
            snap_after = await snapshot_for_verify(ws_url)
            if snap_after != snap_before:
                return True, f"clicked (AUI declarative fire) '{desc}' action='{info.get('action')}'"
            return False, "AUI fire executed but no visible change"

        all_strategies = [("aui_dispatch", _try_aui_dispatch), ("cdp", _try_cdp), ("js", _try_js), ("vision", _try_vision)]
        available = {s[0] for s in all_strategies}
        # Strategy names that USED to exist but no longer do (cleanup poisoned playbook).
        # 'real_cursor' was removed when Layer 3 was killed; old success entries are stale.
        DEAD_STRATEGIES = {"real_cursor", "blocked_layer3", "all_layers"}
        if pref and pref.get("strategy") in DEAD_STRATEGIES:
            _log(f"playbook prefers '{pref['strategy']}' but that strategy is no longer available — ignoring")
            pref = None
        if pref and pref.get("strategy") in available and pref.get("success_rate", 0) >= 0.7:
            preferred = pref["strategy"]
            _log(f"playbook prefers '{preferred}' (rate {pref['success_rate']:.0%})")
            all_strategies = [s for s in all_strategies if s[0] == preferred] + [s for s in all_strategies if s[0] != preferred]

        for strat_name, fn in all_strategies:
            ok, msg = await fn()
            if ok:
                return _result(strat_name, True, msg)
            _log(f"strategy '{strat_name}' failed: {msg}")
            # Record the per-strategy FAILURE so next run's playbook lookup
            # deprioritizes known-bad strategies for this descriptor. Without
            # this, every run repeats the same dead-end attempts.
            if tab_domain:
                _playbook_record(tab_domain, desc, strat_name, False)

        if allow_real_cursor:
            _log("allow_real_cursor=true ignored — Layer 3 is permanently disabled")

        _playbook_record(tab_domain, desc, "all_failed", False)
        return [TextContent(type="text", text=(
            f"❌ Not interactable via DOM/CDP/vision for '{desc}'.\n"
            f"All Layer 1+2+2.5 strategies failed. WebLoom will not move the OS cursor.\n"
            f"Ask the user to click manually, or try a different selector / wait longer / scroll into view.\n\n"
            f"Trace:\n" + "\n".join(trace)
        ))]

    if name == "fill":
        fields = arguments["fields"]
        js = FILL_JS.replace("FIELDS", json.dumps(fields))
        result = await eval_in_tab(ws_url, js)
        val = result.get("result", {}).get("value", "no result")
        # Record each filled field to playbook with field-type schema captured.
        # The schema (text/email/file/select/url/etc.) is the "what kind of value
        # goes here" knowledge that turns a recorded fill into a reusable template.
        try:
            tab_url_r = await eval_in_tab(ws_url, "location.href")
            tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
            if tab_dom and isinstance(fields, list):
                ok_marker = "ok" in str(val).lower() or "filled" in str(val).lower()
                # Probe each field's input type/role so we capture schema, not just selector.
                schema_js = """JSON.stringify((""" + json.dumps([f.get("selector") for f in fields if isinstance(f, dict) and f.get("selector")]) + """).map(sel => {
                    const el = document.querySelector(sel);
                    if (!el) return {sel, missing: true};
                    return {
                        sel,
                        tag: el.tagName,
                        type: el.type || null,
                        role: el.getAttribute('role') || null,
                        editable: el.isContentEditable || false,
                        placeholder: el.placeholder || null,
                        required: el.required || false,
                        maxLength: el.maxLength > 0 ? el.maxLength : null,
                    };
                }))"""
                schema_r = await eval_in_tab(ws_url, schema_js)
                try:
                    schemas = json.loads(schema_r.get("result", {}).get("value", "[]"))
                except Exception:
                    schemas = []
                schema_by_sel = {s["sel"]: s for s in schemas if isinstance(s, dict) and "sel" in s}

                for f in fields:
                    sel = f.get("selector") if isinstance(f, dict) else None
                    if not sel:
                        continue
                    schema = schema_by_sel.get(sel, {})
                    field_type = (
                        "contenteditable" if schema.get("editable")
                        else schema.get("type")
                        or schema.get("role")
                        or "text"
                    )
                    _playbook_record(tab_dom, f"fill:{sel}", "react_setter", ok_marker,
                                     kind="fill", selector_pattern=sel)
                    # Also write field schema into action_log so Threads can carry it
                    pb = _load_live_playbook_raw()
                    alog = pb.setdefault(tab_dom, {}).setdefault("action_log", {})
                    if f"fill:{sel}" in alog:
                        alog[f"fill:{sel}"]["field_type"] = field_type
                        if schema.get("required"):
                            alog[f"fill:{sel}"]["required"] = True
                        if schema.get("maxLength"):
                            alog[f"fill:{sel}"]["max_length"] = schema["maxLength"]
                        if schema.get("placeholder"):
                            alog[f"fill:{sel}"]["placeholder_hint"] = schema["placeholder"]
                        save_playbook_data(pb)
        except Exception:
            pass
        return [TextContent(type="text", text=val)]

    if name == "eval_js":
        code = arguments["code"]
        result = await eval_in_tab(ws_url, code)
        val = result.get("result", {})

        # Heuristic auto-recording: if the eval_js code includes a fetch() call
        # AND the returned value looks like a successful HTTP response
        # ({ok:true,status:200} or similar), record it as a proven action.
        # This catches sessions that discover endpoints via raw fetch() (the
        # MARSTUDIO 2026-05-21 X-crack pattern) and prevents the discovery
        # from being lost when the session ends.
        try:
            if "fetch(" in code:
                # Try to extract the fetch URL from the code (first quoted URL)
                m = re.search(r"fetch\(\s*[`'\"]([^`'\"]+)[`'\"]", code)
                target_url = m.group(1) if m else None
                # Extract the value the eval returned — could be a dict, string, etc.
                returned = val.get("value") if isinstance(val, dict) else val
                # If returned is a JSON string, parse it
                parsed = None
                if isinstance(returned, str):
                    try:
                        parsed = json.loads(returned)
                    except Exception:
                        parsed = None
                elif isinstance(returned, dict):
                    parsed = returned

                ok_flag = None
                status_code = None
                if isinstance(parsed, dict):
                    if "ok" in parsed:
                        ok_flag = bool(parsed.get("ok"))
                    if "status" in parsed:
                        status_code = parsed.get("status")

                # Record only when we have a credible success signal
                if target_url and ok_flag is True and (status_code is None or (200 <= int(status_code or 0) < 400)):
                    # Resolve full URL if relative
                    full_url = target_url
                    if target_url.startswith("/"):
                        try:
                            origin_r = await eval_in_tab(ws_url, "location.origin")
                            origin = origin_r.get("result", {}).get("value", "")
                            if origin:
                                full_url = origin + target_url
                        except Exception:
                            pass
                    d = domain_from_url(full_url)
                    if d:
                        from urllib.parse import urlparse
                        p = urlparse(full_url)
                        parts = [seg for seg in p.path.split("/") if seg]
                        norm = "/" + "/".join(
                            "{hash}" if (len(seg) >= 16 and seg.isalnum()) else seg
                            for seg in parts
                        ) if parts else "/"
                        # Detect method from code (default POST if body present, else GET)
                        method = "GET"
                        mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", code)
                        if mm:
                            method = mm.group(1).upper()
                        elif "body:" in code or "body :" in code:
                            method = "POST"
                        desc = f"eval_js_fetch:{method} {norm}"
                        _playbook_record(
                            d, desc, "eval_js_fetch", True,
                            kind="xhr_replay", selector_pattern=full_url,
                        )
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps(val, indent=2))]

    if name == "find_tab_by_selector":
        js_test = arguments["js_test"]
        url_filter = arguments.get("url_filter", "").lower()
        tabs = [t for t in get_tabs(port) if t.get("type") == "page"]
        results = []
        for t in tabs:
            if url_filter and url_filter not in t.get("url", "").lower():
                continue
            try:
                r = await eval_in_tab(t["webSocketDebuggerUrl"], f"!!(function(){{ return ({js_test}); }})()")
                matched = r.get("result", {}).get("value", False)
                results.append({"id": t["id"], "title": t.get("title",""), "url": t.get("url",""), "matched": matched})
            except Exception as e:
                results.append({"id": t["id"], "title": t.get("title",""), "url": t.get("url",""), "matched": False, "error": str(e)})
        matched = [r for r in results if r["matched"]]
        summary = f"Scanned {len(results)} tab(s). {len(matched)} matched.\n\n"
        for r in results:
            mark = "✅" if r["matched"] else "❌"
            summary += f"{mark} [{r['id'][:16]}] {r['title'][:40]} — {r['url'][:60]}\n"
        if matched:
            summary += f"\nFirst match tab ID: {matched[0]['id']}"
        return [TextContent(type="text", text=summary)]

    if name == "navigate":
        timeout = float(arguments.get("timeout_seconds", 20))
        target_url = arguments["url"]
        # Capture origin URL so we can record the transition as part of the flow.
        try:
            prev_r = await eval_in_tab(ws_url, "location.href")
            prev_url = prev_r.get("result", {}).get("value", "")
        except Exception:
            prev_url = ""
        msg = await navigate_and_wait_load(ws_url, target_url, timeout=timeout)
        # Record successful page transition. Critical for capturing multi-page flows
        # (KDP wizard /details → /content → /pricing, etc.) as sequence in playbook.
        try:
            from urllib.parse import urlparse
            tgt = domain_from_url(target_url)
            if tgt:
                # Use path as descriptor so different pages on the same domain are distinct.
                path = urlparse(target_url).path or "/"
                _playbook_record(tgt, f"navigate:{path}", "navigate", True,
                                 kind="navigate", selector_pattern=target_url)
                # Reset session state on tab navigation — new page = new flow segment
                # but keep follows chain via the navigate descriptor
        except Exception:
            pass
        return [TextContent(type="text", text=msg)]

    if name == "upload_file":
        selector = arguments["selector"]
        files = arguments.get("files", [])
        click_first = arguments.get("click_first_selector", "")
        drop_target = arguments.get("drop_target_selector", "")
        inject_input = arguments.get("inject_input_selector", "")
        react_input = arguments.get("react_input_selector", "")
        if not files:
            return [TextContent(type="text", text="upload_file: 'files' is empty.")]
        missing = [f for f in files if not Path(f).is_file()]
        if missing:
            return [TextContent(type="text", text=f"upload_file: files not found: {missing}")]
        abs_files = [str(Path(f).resolve()) for f in files]

        # ── STRATEGY E: shadow-DOM-aware + native HTMLInputElement setter + React onChange ──
        # Solves LinkedIn-style composer modals: file input lives inside a
        # shadow root, AND React's controlled-input observer doesn't trust
        # Strategies B/D because they go through DOM.setFileInputFiles or
        # synthetic events. The native HTMLInputElement.files setter is the
        # path React DOES track via its descriptor-injection magic.
        if react_input:
            import base64, mimetypes
            file_blobs = []
            total = 0
            for fp in abs_files:
                with open(fp, "rb") as f:
                    data = f.read()
                if total + len(data) > 25 * 1024 * 1024:
                    return [TextContent(type="text", text=json.dumps({"ok": False, "error": "Strategy E: combined files exceed 25MB cap"}, indent=2))]
                total += len(data)
                mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                file_blobs.append({
                    "name": Path(fp).name,
                    "mime": mime,
                    "b64": base64.b64encode(data).decode("ascii"),
                })

            js = """(async function(sel, blobs) {
                function deepQuery(root, s) {
                    const direct = root.querySelector(s);
                    if (direct) return direct;
                    const all = root.querySelectorAll('*');
                    for (const node of all) {
                        if (node.shadowRoot) {
                            const inner = deepQuery(node.shadowRoot, s);
                            if (inner) return inner;
                        }
                    }
                    return null;
                }
                const inp = deepQuery(document, sel);
                if (!inp) return JSON.stringify({ok:false, error:'input not found in light+shadow DOM: ' + sel});
                if (inp.tagName !== 'INPUT' || inp.type !== 'file') {
                    return JSON.stringify({ok:false, error:'matched but not <input type=file>: ' + inp.tagName + '/' + inp.type});
                }

                // Build File objects in page context from base64 payloads
                const fileObjs = [];
                for (const b of blobs) {
                    const bin = atob(b.b64);
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    fileObjs.push(new File([bytes], b.name, { type: b.mime }));
                }
                const dt = new DataTransfer();
                fileObjs.forEach(f => dt.items.add(f));

                // CRITICAL: use the native prototype setter, not direct assignment.
                // React's controlled-input observer tracks the descriptor's setter
                // and ignores changes made via direct assignment.
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files').set;
                setter.call(inp, dt.files);

                // Fire input + change so legacy listeners + React's onChange both see it
                inp.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true, composed: true }));

                // ALSO walk fiber to invoke onChange directly — for handler-gated implementations
                let onChangeCalled = false;
                const propsKey = Object.keys(inp).find(k => k.startsWith('__reactProps'));
                if (propsKey && inp[propsKey] && typeof inp[propsKey].onChange === 'function') {
                    try {
                        inp[propsKey].onChange({
                            target: inp,
                            currentTarget: inp,
                            preventDefault: () => {},
                            stopPropagation: () => {},
                            nativeEvent: { target: inp },
                            bubbles: true,
                        });
                        onChangeCalled = true;
                    } catch (e) {}
                }
                if (!onChangeCalled) {
                    const fiberKey = Object.keys(inp).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
                    if (fiberKey) {
                        let fiber = inp[fiberKey];
                        let hops = 0;
                        while (fiber && hops < 20) {
                            const p = fiber.memoizedProps || fiber.pendingProps;
                            if (p && typeof p.onChange === 'function') {
                                try {
                                    p.onChange({
                                        target: inp, currentTarget: inp,
                                        preventDefault: () => {}, stopPropagation: () => {},
                                        nativeEvent: { target: inp }, bubbles: true,
                                    });
                                    onChangeCalled = true;
                                    break;
                                } catch (e) {}
                            }
                            fiber = fiber.return;
                            hops++;
                        }
                    }
                }

                return JSON.stringify({
                    ok: true,
                    readback_len: inp.files ? inp.files.length : 0,
                    react_onchange_invoked: onChangeCalled,
                    file_names: Array.from(inp.files || []).map(f => f.name),
                });
            })(""" + json.dumps(react_input) + ", " + json.dumps(file_blobs) + ")"
            r = await eval_in_tab(ws_url, js)
            val = r.get("result", {}).get("value", "{}")
            try:
                cur = await eval_in_tab(ws_url, "location.href")
                dom = domain_from_url(cur.get("result", {}).get("value", ""))
                parsed = json.loads(val) if isinstance(val, str) else {}
                if dom:
                    _playbook_record(dom, f"upload_file:E:{react_input}", "strategy_e_shadow_react",
                                     bool(parsed.get("ok")), kind="upload", selector_pattern=react_input)
            except Exception:
                pass
            return [TextContent(type="text", text=f"upload_file (Strategy E — shadow+React): {val}")]

        # Strategy D: re-inject file bytes as real File objects and assign via
        # DataTransfer.files onto the actual <input type=file>. Solves the
        # label-wrapped-hidden-input pattern (D2D, KDP) where:
        #   - Strategy A drops CDP on Windows (file picker blocks UI thread)
        #   - Strategy B's setFileInputFiles result is invisible to page JS
        #   - Strategy C has no drop listener to fire against
        # By constructing the File entirely in page-JS context, the FileList
        # is visible to page scripts (no Chromium "set programmatically" filter).
        #
        # AUTO STRATEGY E (AjaxInput-aware): when the input matches the AjaxInput
        # pattern (id ends with `-AjaxInput`, or `.fileuploader` parent, or the
        # uploader span has class `a-declarative`), we install an XHR observer
        # BEFORE the inject so we capture the upload XHR that AjaxInput fires
        # on the change event. Then we report the upload outcome (URL + status
        # + response) so the caller knows whether the upload actually committed
        # to Amazon's backend, not just whether the change event fired.
        # We also harden the File metadata (lastModified, mime sniff) to match
        # a real picker-supplied File, which is enough to make Amazon's
        # multipart upload succeed in most cases.
        if inject_input:
            import base64, mimetypes
            file_payloads = []
            total_bytes = 0
            for f in abs_files:
                p = Path(f)
                data = p.read_bytes()
                total_bytes += len(data)
                if total_bytes > 25_000_000:
                    return [TextContent(type="text", text="upload_file (inject): >25MB combined; too large for inline injection.")]
                # Real-picker-like metadata: lastModified from fs mtime,
                # mime by extension AND magic-byte sniff fallback.
                mime = mimetypes.guess_type(p.name)[0]
                if not mime:
                    # Magic-byte sniffs for common KDP/D2D upload types
                    head = data[:8]
                    if head.startswith(b"PK\x03\x04"):
                        mime = "application/zip"  # docx/epub/zip all start with PK
                        if p.suffix.lower() == ".docx":
                            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                        elif p.suffix.lower() == ".epub":
                            mime = "application/epub+zip"
                    elif head.startswith(b"%PDF"):
                        mime = "application/pdf"
                    else:
                        mime = "application/octet-stream"
                file_payloads.append({
                    "name": p.name,
                    "type": mime,
                    "last_modified": int(p.stat().st_mtime * 1000),
                    "b64": base64.b64encode(data).decode("ascii"),
                })
            payloads_js = json.dumps(file_payloads)
            input_sel = json.dumps(inject_input)
            inject_js = f"""(async function() {{
                const input = document.querySelector({input_sel});
                if (!input) return JSON.stringify({{ok:false, error:'input not found: '+{input_sel}}});

                // ── AjaxInput detection ──────────────────────────────────────
                const isAjaxInput = (
                    /-AjaxInput$/i.test(input.id || '') ||
                    !!input.closest('.fileuploader') ||
                    !!input.closest('[id$="-uploader"].a-declarative')
                );

                // ── Strategy E shim: install XHR observer BEFORE inject ─────
                // Captures the upload XHR that AjaxInput fires on change. We
                // record method, URL, status, response so the caller sees if
                // Amazon's backend actually accepted the file.
                if (isAjaxInput && !window.__webloomXhrObserver) {{
                    window.__webloomXhrObserver = {{ captured: [] }};
                    const _open = XMLHttpRequest.prototype.open;
                    const _send = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
                        this.__wlMethod = method; this.__wlUrl = url;
                        return _open.call(this, method, url, ...rest);
                    }};
                    XMLHttpRequest.prototype.send = function(body) {{
                        const url = this.__wlUrl || '';
                        const isUploadish = /upload|file|asset|interior|content|cover/i.test(url)
                                          && this.__wlMethod === 'POST';
                        if (isUploadish) {{
                            const idx = window.__webloomXhrObserver.captured.length;
                            window.__webloomXhrObserver.captured.push({{
                                method: this.__wlMethod, url, ts_start: Date.now(),
                                body_type: body && body.constructor && body.constructor.name,
                                status: null, response: null, ts_end: null, error: null,
                            }});
                            const onDone = () => {{
                                const slot = window.__webloomXhrObserver.captured[idx];
                                if (!slot) return;
                                slot.status = this.status;
                                slot.response = (this.responseText || '').slice(0, 500);
                                slot.ts_end = Date.now();
                            }};
                            this.addEventListener('load', onDone);
                            this.addEventListener('error', () => {{
                                const slot = window.__webloomXhrObserver.captured[idx];
                                if (slot) {{ slot.error = 'network error'; slot.ts_end = Date.now(); }}
                            }});
                        }}
                        return _send.call(this, body);
                    }};
                }}

                const payloads = {payloads_js};
                const files = payloads.map(p => {{
                    const bin = atob(p.b64);
                    const buf = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
                    // Real-picker-like File: include lastModified to match what a
                    // user-picked file would carry. Some uploaders sniff this.
                    return new File([buf], p.name, {{
                        type: p.type,
                        lastModified: p.last_modified || Date.now(),
                    }});
                }});
                const dt = new DataTransfer();
                for (const f of files) dt.items.add(f);
                try {{ input.files = dt.files; }} catch(e) {{
                    return JSON.stringify({{ok:false, error:'input.files assignment refused: '+e.message}});
                }}
                input.dispatchEvent(new Event('input',  {{bubbles: true, composed: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                const lbl = input.closest('label, [class*=upload i], [class*=drop i], [data-testid*=upload i]');
                if (lbl && lbl !== input) {{
                    lbl.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
                }}

                // ── Strategy E: wait briefly for the XHR to fly + report ────
                let xhrInfo = null;
                if (isAjaxInput && window.__webloomXhrObserver) {{
                    const deadline = Date.now() + 15000; // up to 15s for upload to start + return
                    while (Date.now() < deadline) {{
                        const captured = window.__webloomXhrObserver.captured;
                        const recent = captured[captured.length - 1];
                        if (recent && recent.ts_end) {{ xhrInfo = recent; break; }}
                        await new Promise(r => setTimeout(r, 250));
                    }}
                    // Even if no end yet, return the start info so caller sees something fired
                    if (!xhrInfo) {{
                        const captured = window.__webloomXhrObserver.captured;
                        xhrInfo = captured[captured.length - 1] || null;
                    }}
                }}

                return JSON.stringify({{
                    ok: true,
                    strategy: isAjaxInput ? 'E (ajaxinput + xhr observer)' : 'D (inject + DataTransfer.files)',
                    is_ajaxinput: isAjaxInput,
                    injected: files.length,
                    readback_len: input.files ? input.files.length : 0,
                    first_name: input.files && input.files[0] ? input.files[0].name : null,
                    first_size: input.files && input.files[0] ? input.files[0].size : null,
                    upload_xhr: xhrInfo,
                }});
            }})()"""
            r = await eval_in_tab(ws_url, inject_js)
            val = r.get("result", {}).get("value", "{}")
            # Try to parse + record to playbook if AjaxInput + 2xx upload
            try:
                parsed = json.loads(val)
                if parsed.get("is_ajaxinput"):
                    xhr = parsed.get("upload_xhr") or {}
                    status = xhr.get("status")
                    # Resolve domain from the current tab URL
                    try:
                        tab_url_r = await eval_in_tab(ws_url, "location.href")
                        cur_url = tab_url_r.get("result", {}).get("value", "")
                        domain = domain_from_url(cur_url)
                    except Exception:
                        domain = None
                    if status and 200 <= status < 300:
                        _playbook_record(domain or "unknown", f"upload_file:{Path(inject_input).name if inject_input else inject_input}",
                                         "strategy_e", True, kind="upload", selector_pattern=inject_input)
                    elif status:
                        # Capture as coverage gap with clear classification
                        record_coverage_gap(
                            domain or "unknown",
                            f"upload_file:{inject_input}",
                            f"AjaxInput upload XHR returned HTTP {status} — file metadata or CSRF token may differ from real picker",
                            classification="engine_fix_needed",
                            status="open",
                        )
            except Exception:
                pass
            return [TextContent(type="text", text=f"upload_file result: {val}")]

        # Strategy C: synthetic drag-and-drop on a target element.
        # Reads file bytes → injects as real File objects → dispatches native drag events.
        # Beats React-controlled inputs that ignore programmatically-set FileList.
        # Use this when Strategy A drops CDP on Windows and Strategy B doesn't wake React.
        if drop_target:
            import base64, mimetypes
            file_payloads = []
            total_bytes = 0
            for f in abs_files:
                p = Path(f)
                data = p.read_bytes()
                total_bytes += len(data)
                if total_bytes > 25_000_000:
                    return [TextContent(type="text", text=f"upload_file (drop): >25MB combined; too large for inline JS injection. Use Strategy A with click_first_selector instead.")]
                file_payloads.append({
                    "name": p.name,
                    "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                    "b64": base64.b64encode(data).decode("ascii"),
                })
            payloads_js = json.dumps(file_payloads)
            target_js = json.dumps(drop_target)
            drop_js = f"""(async function() {{
                const target = document.querySelector({target_js});
                if (!target) return JSON.stringify({{ok:false, error:'drop target not found'}});
                const payloads = {payloads_js};
                const files = payloads.map(p => {{
                    const bin = atob(p.b64);
                    const buf = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
                    return new File([buf], p.name, {{type: p.type}});
                }});
                const dt = new DataTransfer();
                for (const f of files) dt.items.add(f);
                const fire = (type, x, y) => {{
                    const ev = new DragEvent(type, {{
                        bubbles: true, cancelable: true, composed: true,
                        dataTransfer: dt, clientX: x, clientY: y,
                    }});
                    target.dispatchEvent(ev);
                }};
                const r = target.getBoundingClientRect();
                const cx = r.left + r.width/2, cy = r.top + r.height/2;
                fire('dragenter', cx, cy);
                fire('dragover',  cx, cy);
                fire('drop',      cx, cy);
                return JSON.stringify({{ok:true, dropped: files.length, target_tag: target.tagName, target_classes: target.className.toString().slice(0,120)}});
            }})()"""
            r = await eval_in_tab(ws_url, drop_js)
            val = r.get("result", {}).get("value", "{}")
            try:
                parsed = json.loads(val)
                if parsed.get("ok"):
                    tab_url_r = await eval_in_tab(ws_url, "location.href")
                    tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                    if tab_dom:
                        _playbook_record(tab_dom, f"upload_file:drop:{drop_target}", "strategy_c", True,
                                         kind="upload", selector_pattern=drop_target)
            except Exception:
                pass
            return [TextContent(type="text", text=f"Strategy C (synthetic drop) on '{drop_target}': {val}")]

        # Strategy A: file-chooser interception. Works on any site, including
        # ones that hide the <input type=file> behind a styled button (D2D, KDP).
        # Enable interception → click the visible trigger → CDP emits
        # Page.fileChooserOpened → we resolve it with the files.
        if click_first:
            try:
                await cdp_send(ws_url, "Page.enable")
                await cdp_send(ws_url, "Page.setInterceptFileChooserDialog", {"enabled": True})
                conn = await _get_conn(ws_url)
                wait_task = asyncio.create_task(conn.wait_event("Page.fileChooserOpened", timeout=10))
                # Click the visible trigger via JS
                click_js = f"""(function() {{
                    const el = document.querySelector({json.dumps(click_first)});
                    if (!el) return 'not found';
                    el.scrollIntoView({{block:'center'}});
                    el.click();
                    return 'clicked';
                }})()"""
                cr = await eval_in_tab(ws_url, click_js)
                if cr.get("result", {}).get("value") != "clicked":
                    wait_task.cancel()
                    await cdp_send(ws_url, "Page.setInterceptFileChooserDialog", {"enabled": False})
                    return [TextContent(type="text", text=f"upload_file: trigger '{click_first}' not found.")]
                evt = await wait_task
                params = evt.get("params", {})
                backend_node_id = params.get("backendNodeId")
                if backend_node_id:
                    await cdp_send(ws_url, "DOM.setFileInputFiles", {"files": abs_files, "backendNodeId": backend_node_id})
                    await cdp_send(ws_url, "Page.setInterceptFileChooserDialog", {"enabled": False})
                    try:
                        tab_url_r = await eval_in_tab(ws_url, "location.href")
                        tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                        if tab_dom:
                            _playbook_record(tab_dom, f"upload_file:click:{click_first}", "strategy_a", True,
                                             kind="upload", selector_pattern=click_first)
                    except Exception:
                        pass
                    return [TextContent(type="text", text=f"Uploaded {len(abs_files)} file(s) via file-chooser intercept (trigger: {click_first})")]
                await cdp_send(ws_url, "Page.setInterceptFileChooserDialog", {"enabled": False})
            except asyncio.TimeoutError:
                try:
                    await cdp_send(ws_url, "Page.setInterceptFileChooserDialog", {"enabled": False})
                except Exception:
                    pass
                return [TextContent(type="text", text=f"upload_file: clicked '{click_first}' but no fileChooser event fired in 10s — the trigger may not open a real picker.")]

        # Strategy B: resolve the input via Runtime.evaluate → objectId.
        # Beats DOM.querySelector — handles shadow DOM, hidden/offscreen inputs,
        # iframes, and inputs not yet attached to the main document.
        js_locate = f"""(function() {{
            const sel = {json.dumps(selector)};
            // Try direct selector
            let el = document.querySelector(sel);
            if (el) return el;
            // Pierce open shadow roots
            const walk = (root) => {{
                const found = root.querySelector(sel);
                if (found) return found;
                const all = root.querySelectorAll('*');
                for (const node of all) {{
                    if (node.shadowRoot) {{
                        const f = walk(node.shadowRoot);
                        if (f) return f;
                    }}
                }}
                return null;
            }};
            el = walk(document);
            if (el) return el;
            // Try same-origin iframes
            for (const fr of document.querySelectorAll('iframe')) {{
                try {{
                    const f = fr.contentDocument && fr.contentDocument.querySelector(sel);
                    if (f) return f;
                }} catch(e) {{}}
            }}
            return null;
        }})()"""
        r = await cdp_send(ws_url, "Runtime.evaluate", {
            "expression": js_locate,
            "returnByValue": False,
        })
        result = r.get("result", {})
        object_id = result.get("objectId")
        if not object_id or result.get("subtype") == "null":
            return [TextContent(type="text", text=(
                f"upload_file: no element matches selector '{selector}' (tried main DOM, all shadow roots, same-origin iframes).\n"
                f"If the page hides the real input behind a styled button, pass `click_first_selector` "
                f"to the upload button instead — we'll intercept the file picker."
            ))]
        try:
            await cdp_send(ws_url, "DOM.setFileInputFiles", {"files": abs_files, "objectId": object_id})
            # React-aware fallback: many controlled-input frameworks (React, Vue)
            # don't pick up the native change event when files are set via CDP.
            # Re-dispatch input/change with bubbles, plus poke any drop-zone parent.
            await cdp_send(ws_url, "Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": """function() {
                    this.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                    this.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                    // Walk up to a likely drop-zone ancestor and signal it too
                    let p = this.parentElement, hops = 0;
                    while (p && hops < 6) {
                        if (p.matches && (p.matches('[class*=drop i],[class*=upload i],[role=button],[data-testid*=upload i]'))) {
                            p.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                            break;
                        }
                        p = p.parentElement;
                        hops++;
                    }
                }""",
                "returnByValue": True,
            })
        finally:
            try:
                await cdp_send(ws_url, "Runtime.releaseObject", {"objectId": object_id})
            except Exception:
                pass
        return [TextContent(type="text", text=(
            f"Uploaded {len(abs_files)} file(s) to {selector} via Strategy B (objectId + event re-dispatch).\n"
            f"If the page UI doesn't show the file as uploaded, the framework is ignoring synthetic events — "
            f"retry with `click_first_selector` pointed at the visible upload button (Strategy A — file-chooser intercept)."
        ))]

    if name == "detect_anti_bot":
        r = await eval_in_tab(ws_url, ANTI_BOT_JS)
        try:
            data = json.loads(r.get("result", {}).get("value", "{}"))
        except Exception:
            return [TextContent(type="text", text="detect_anti_bot: parse failed.")]
        verdict = data.get("verdict", "normal")
        signals = data.get("signals", [])
        page = data.get("page", {})
        if verdict == "normal":
            return [TextContent(type="text", text=f"✅ Anti-bot: normal page. bodyLen={page.get('bodyLen')} interactive={page.get('interactiveCount')}")]
        lines = [f"🚧 Anti-bot detected: **{verdict}**"]
        for s in signals:
            lines.append(f"  • {s.get('type')} (confidence: {s.get('confidence', '?')})")
        lines.append(f"\nPage: {page.get('title','?')}  bodyLen={page.get('bodyLen')}  interactive={page.get('interactiveCount')}")
        lines.append("\nRecommendation: this site needs the user's REAL Chrome with their fingerprint/cookies (not headless). Skip auto-Thread generation; surface for manual capture.")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "framework_detect":
        r = await eval_in_tab(ws_url, FRAMEWORK_DETECT_JS)
        try:
            data = json.loads(r.get("result", {}).get("value", "{}"))
        except Exception:
            return [TextContent(type="text", text="framework_detect: parse failed.")]
        frameworks = data.get("frameworks", [])
        indicators = data.get("indicators", {})
        page = data.get("page", {})
        lines = [f"## Framework detection: {page.get('title', '?')}", f"**URL:** {page.get('url','?')}", ""]
        lines.append(f"**Primary:** {data.get('primary', 'vanilla')}")
        if frameworks:
            lines.append(f"**All detected:** {', '.join(frameworks)}")
        lines.append("")
        lines.append("**DOM indicators:**")
        for k, v in indicators.items():
            lines.append(f"  • {k}: {v}")
        lines.append("")
        lines.append("**Strategy hints:**")
        if "amazon-aui" in frameworks:
            lines.append("  → Modal saves / form submits: use `aui_dispatch` with 'a:click' event")
        if any("react" in f for f in frameworks):
            lines.append("  → Controlled inputs: use `react_force_change` if normal fill fails")
        if "redux-global-store" in frameworks or "redux-devtools-installed" in frameworks:
            lines.append("  → State commits: try `react_inspect_store` / `redux_dispatch`")
        if "backbone-" in str(frameworks):
            lines.append("  → Read-only state: `backbone_inspect`")
        if any(f in frameworks for f in ("radix", "headlessui")):
            lines.append("  → Dropdowns listen for mousedown — engine handles via Stage 1 actionability")
        if indicators.get("has_label_wrapped_file"):
            lines.append("  → Uploads: use Strategy D (`upload_file(inject_input_selector=...)`)")
        if indicators.get("has_drop_zone"):
            lines.append("  → Or Strategy C (`upload_file(drop_target_selector=...)`)")
        if indicators.get("iframe_count", 0) > 0:
            lines.append(f"  → {indicators['iframe_count']} iframe(s) on page — cross-origin content uncrossable by default")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "wait_for_idle":
        timeout = float(arguments.get("timeout_seconds", 10))
        try:
            await cdp_send(ws_url, "Page.enable")
            await cdp_send(ws_url, "Page.setLifecycleEventsEnabled", {"enabled": True})
        except Exception:
            pass
        conn = await _get_conn(ws_url)
        deadline = asyncio.get_event_loop().time() + timeout
        got_idle = False
        while asyncio.get_event_loop().time() < deadline:
            remaining = max(0.5, deadline - asyncio.get_event_loop().time())
            try:
                ev = await conn.wait_event("Page.lifecycleEvent", timeout=remaining)
                if ev.get("params", {}).get("name") == "networkAlmostIdle":
                    got_idle = True
                    break
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        return [TextContent(type="text", text=(
            f"✅ networkAlmostIdle reached" if got_idle else
            f"⏱️ Timed out after {timeout}s waiting for networkAlmostIdle — proceeding anyway"
        ))]

    if name == "seed_from_tab":
        import time as _t
        # 1. Framework detect
        fr = await eval_in_tab(ws_url, FRAMEWORK_DETECT_JS)
        try:
            fr_data = json.loads(fr.get("result", {}).get("value", "{}"))
        except Exception:
            fr_data = {}
        # 2. Anti-bot check
        ab = await eval_in_tab(ws_url, ANTI_BOT_JS)
        try:
            ab_data = json.loads(ab.get("result", {}).get("value", "{}"))
        except Exception:
            ab_data = {}
        # 3. AX scan
        sc = await eval_in_tab(ws_url, SCAN_AX_JS)
        try:
            sc_data = json.loads(sc.get("result", {}).get("value", "{}"))
        except Exception:
            sc_data = {}
        # 4. Optional network capture
        cap_seconds = float(arguments.get("capture_seconds", 0) or 0)
        endpoints = []
        if cap_seconds > 0:
            await cdp_send(ws_url, "Network.enable")
            conn = await _get_conn(ws_url)
            captured = []
            def _on_req(msg):
                p = msg.get("params", {})
                req = p.get("request", {})
                captured.append({"url": req.get("url"), "method": req.get("method"), "type": p.get("type")})
            conn.subscribe("Network.requestWillBeSent", _on_req)
            await asyncio.sleep(cap_seconds)
            conn.unsubscribe("Network.requestWillBeSent", _on_req)
            # Filter to likely interesting endpoints (POSTs + non-static GETs)
            for e in captured:
                u = (e.get("url") or "").lower()
                m = e.get("method", "").upper()
                if m == "POST" or (m == "GET" and "/api/" in u):
                    if not any(x in u for x in (".png", ".jpg", ".css", ".woff", ".svg", "google-analytics", "doubleclick")):
                        endpoints.append(e)

        # Build the Thread
        url = (fr_data.get("page") or {}).get("url") or (sc_data.get("url") or "")
        domain = domain_from_url(url)
        verdict = ab_data.get("verdict", "normal")
        frameworks = fr_data.get("frameworks", []) or []
        indicators = fr_data.get("indicators", {}) or {}

        notes = []
        quirks = {}
        if verdict != "normal":
            notes.append(f"⚠️  Anti-bot detected: {verdict}. This Thread was generated in headless mode and may be incomplete. Real Chrome with the user's fingerprint may be required.")
        if any("react" in f for f in frameworks):
            notes.append("React detected — controlled inputs may need `react_force_change` as fallback.")
        if "amazon-aui" in frameworks:
            notes.append("Amazon AUI detected — modal saves use `aui_dispatch(event='a:click', ...)`.")
            quirks["aui_modal_save"] = "use aui_dispatch"
        if "redux-global-store" in frameworks or "redux-devtools-installed" in frameworks:
            notes.append("Redux store accessible — try `react_inspect_store` / `redux_dispatch` for state commits.")
        if indicators.get("has_label_wrapped_file"):
            notes.append("Label-wrapped hidden file input — use `upload_file(inject_input_selector=...)` Strategy D.")
            quirks["uploads"] = "Strategy D"
        if indicators.get("has_drop_zone"):
            notes.append("Explicit drop zone present — Strategy C may also work.")
        if indicators.get("iframe_count", 0) > 0:
            notes.append(f"{indicators['iframe_count']} iframe(s) present — cross-origin may be uncrossable.")

        thread = {
            "domain": domain,
            "name": f"{domain} Thread",
            "version": "0.1.0",
            "author": "seed_from_tab (current session)",
            "license": "cc-by",
            "tier": "starter",
            "framework": fr_data.get("primary", "vanilla"),
            "frameworks_detected": frameworks,
            "anti_bot_verdict": verdict,
            "anti_bot_signals": ab_data.get("signals", []),
            "default_strategy": "js" if "amazon-aui" in frameworks else "cdp",
            "notes": notes,
            "quirks": quirks,
            "ax_snapshot": sc_data.get("lines", [])[:30],
            "page_indicators": indicators,
            "captured_endpoints": endpoints,
            "created_at": int(_t.time()),
            "created_by": "seed_from_tab",
            "source_url": url,
        }

        if arguments.get("save", False):
            THREADS_DIR.mkdir(parents=True, exist_ok=True)
            out_path = THREADS_DIR / f"{domain}.thread.json"
            out_path.write_text(json.dumps(thread, indent=2), encoding="utf-8")
            return [TextContent(type="text", text=(
                f"Seeded Thread for **{domain}** → {out_path}\n"
                f"  framework: {thread['framework']}  ·  anti-bot: {verdict}  ·  ax elements: {len(thread['ax_snapshot'])}  ·  endpoints captured: {len(endpoints)}\n\n"
                f"```json\n{json.dumps(thread, indent=2)[:1500]}{'...' if len(json.dumps(thread)) > 1500 else ''}\n```"
            ))]
        return [TextContent(type="text", text=(
            f"Generated Thread draft for **{domain}** (not saved — pass save=true to write):\n\n"
            f"```json\n{json.dumps(thread, indent=2)[:2500]}{'...' if len(json.dumps(thread)) > 2500 else ''}\n```"
        ))]

    if name == "detect_blocker":
        r = await eval_in_tab(ws_url, DETECT_BLOCKER_JS)
        try:
            data = json.loads(r.get("result", {}).get("value", "{}"))
        except Exception:
            return [TextContent(type="text", text="detect_blocker: failed to parse page state.")]
        blockers = data.get("blockers", [])
        if not blockers:
            return [TextContent(type="text", text=f"✅ No blockers detected. Page ready={data.get('ready')}.")]
        lines = [f"🚧 **{len(blockers)} blocker(s) detected:**\n"]
        for b in blockers:
            t = b.get("type")
            kind = b.get("kind", "")
            sel = b.get("selector", "")
            line = f"  • **{t}**" + (f" ({kind})" if kind else "") + (f" → selector `{sel}`" if sel else "")
            lines.append(line)
            if t == "captcha":
                lines.append("    → action: call `pause_for_human` — CAPTCHA needs a human.")
            elif t == "2fa" and kind == "totp":
                lines.append("    → action: call `auth_totp(secret=...)` if you have the shared secret.")
            elif t == "2fa" and kind in ("sms", "email-code"):
                lines.append(f"    → action: read the {kind} code (Gmail MCP for email, user for SMS) then `fill`.")
            elif t == "2fa" and kind in ("push", "hardware-key"):
                lines.append(f"    → action: call `pause_for_human` — {kind} requires the user.")
            elif t == "login-wall":
                lines.append("    → action: `fill` credentials then `click` submit.")
            elif t == "verification":
                lines.append("    → action: investigate page; likely human required.")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "auth_totp":
        try:
            import pyotp
        except ImportError:
            return [TextContent(type="text", text="auth_totp: pyotp not installed. Run: pip install pyotp")]
        secret = arguments["secret"].replace(" ", "").upper()
        try:
            code = pyotp.TOTP(secret).now()
        except Exception as e:
            return [TextContent(type="text", text=f"auth_totp: invalid secret ({e})")]
        sel = arguments.get("selector", "")
        submit = bool(arguments.get("submit", False))
        if not sel:
            sel = ('input[autocomplete="one-time-code"], input[name*="otp" i], '
                   'input[name*="2fa" i], input[name*="totp" i], '
                   'input[id*="otp" i], input[id*="2fa" i]')
        fill_js = """(function(sel, value, submit) {
            const el = document.querySelector(sel);
            if (!el) return JSON.stringify({ok:false, error:'no element matches: '+sel});
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            if (submit) {
                const form = el.closest('form');
                if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
                else el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));
            }
            return JSON.stringify({ok:true, filled: value.length + ' digits'});
        })(""" + json.dumps(sel) + ", " + json.dumps(code) + ", " + ("true" if submit else "false") + ")"
        result = await eval_in_tab(ws_url, fill_js)
        val = result.get("result", {}).get("value", "{}")
        return [TextContent(type="text", text=f"TOTP code generated (****{code[-2:]}). Fill result: {val}")]

    if name == "export_profile":
        out_path = Path(arguments["out_path"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cookies_res = await cdp_browser_send(port, "Storage.getCookies")
        cookies = cookies_res.get("cookies", [])
        out_path.write_text(json.dumps({"cookies": cookies}, indent=2))
        return [TextContent(type="text", text=f"Exported {len(cookies)} cookie(s) to {out_path}")]

    if name == "import_profile":
        in_path = Path(arguments["in_path"])
        if not in_path.is_file():
            return [TextContent(type="text", text=f"import_profile: file not found: {in_path}")]
        data = json.loads(in_path.read_text())
        cookies = data.get("cookies", [])
        if not cookies:
            return [TextContent(type="text", text="import_profile: no cookies in file.")]
        await cdp_browser_send(port, "Storage.setCookies", {"cookies": cookies})
        return [TextContent(type="text", text=f"Imported {len(cookies)} cookie(s) from {in_path}")]

    if name == "capture_network_start":
        conn = await _get_conn(ws_url)
        await conn.send("Network.enable")
        if ws_url in _network_active:
            return [TextContent(type="text", text="Already capturing network for this tab.")]
        buf: list = []
        index: dict[str, dict] = {}

        def on_request(msg):
            p = msg.get("params", {})
            rid = p.get("requestId")
            req = p.get("request", {})
            entry = {
                "requestId": rid,
                "url": req.get("url"),
                "method": req.get("method"),
                "type": p.get("type"),
                "started_ms": int(p.get("timestamp", 0) * 1000),
                # Full request headers + body — captured at this stage so replay always works.
                # The MARSTUDIO 2026-05-21 X-crack incident proved we MUST persist these.
                "request_headers": dict(req.get("headers") or {}),
                "request_body": req.get("postData"),
                "has_post_data": bool(req.get("hasPostData")),
            }
            index[rid] = entry
            buf.append(entry)

        def on_request_extra(msg):
            # Network.requestWillBeSentExtraInfo carries auth headers (Cookie,
            # x-csrf-token, x-client-transaction-id, etc.) that the regular event
            # redacts. Merge in.
            p = msg.get("params", {})
            rid = p.get("requestId")
            entry = index.get(rid)
            if not entry:
                return
            extra = p.get("headers") or {}
            merged = dict(entry.get("request_headers") or {})
            for k, v in extra.items():
                merged[k] = v
            entry["request_headers"] = merged

        def on_response(msg):
            p = msg.get("params", {})
            rid = p.get("requestId")
            res = p.get("response", {})
            entry = index.get(rid)
            if not entry:
                return
            entry["status"] = res.get("status")
            entry["mimeType"] = res.get("mimeType")
            entry["from_cache"] = res.get("fromDiskCache") or res.get("fromServiceWorker")
            entry["response_headers"] = dict(res.get("headers") or {})

        conn.subscribe("Network.requestWillBeSent", on_request)
        conn.subscribe("Network.requestWillBeSentExtraInfo", on_request_extra)
        conn.subscribe("Network.responseReceived", on_response)
        _network_buffers[ws_url] = buf
        _network_active[ws_url] = {
            "req_cb": on_request,
            "req_extra_cb": on_request_extra,
            "res_cb": on_response,
        }
        return [TextContent(type="text", text="📡 Network capture started (full headers + body).")]

    if name in ("capture_network_stop", "get_captured_requests"):
        buf = _network_buffers.get(ws_url, [])
        url_filter = (arguments.get("url_filter") or "").lower()
        full = bool(arguments.get("full"))
        filtered = [e for e in buf if not url_filter or url_filter in (e.get("url") or "").lower()]
        if name == "capture_network_stop":
            try:
                conn = await _get_conn(ws_url)
                active = _network_active.pop(ws_url, None)
                if active:
                    conn.unsubscribe("Network.requestWillBeSent", active["req_cb"])
                    extra = active.get("req_extra_cb")
                    if extra:
                        conn.unsubscribe("Network.requestWillBeSentExtraInfo", extra)
                    conn.unsubscribe("Network.responseReceived", active["res_cb"])
                await conn.send("Network.disable")
            except Exception:
                pass
            _network_buffers.pop(ws_url, None)
            header = f"📴 Capture stopped — {len(filtered)} request(s) returned"
        else:
            header = f"📡 Capture active — {len(filtered)} request(s) so far"
        # Full-mode output: JSON with full headers + body. Use when you need
        # to replay a captured call (auth tokens, x-csrf, transaction-id, body shape).
        if full:
            out = {"summary": header, "count": len(filtered), "requests": filtered[-50:]}
            return [TextContent(type="text", text=json.dumps(out, indent=2, default=str))]
        # Compact output: one line per request
        lines = [header + (f" (filter '{url_filter}')" if url_filter else "") + "\n"]
        for e in filtered[-200:]:
            status = e.get("status", "—")
            mime = (e.get("mimeType") or "")[:30]
            url = (e.get("url") or "")[:120]
            lines.append(f"  [{status}] {e.get('method','?'):4s} {e.get('type','?'):10s} {mime:30s} {url}")
        lines.append("\n(call again with full=true to get headers + body for replay)")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "scan_tab_diff":
        result = await eval_in_tab(ws_url, SCAN_JS)
        try:
            data = json.loads(result.get("result", {}).get("value", "{}"))
        except Exception:
            return [TextContent(type="text", text="scan_tab_diff: scan failed.")]
        prev = _diff_fingerprints.get(ws_url)
        _diff_fingerprints[ws_url] = data

        def _idx(items, keyfn):
            return {keyfn(i): i for i in items}

        if prev is None:
            url = data.get("url", "")
            return [TextContent(type="text", text=(
                f"## First scan (baseline saved) — {data.get('title')}\n**URL:** {url}\n"
                f"Captured {len(data.get('buttons', []))} buttons, {len(data.get('inputs', []))} inputs.\n"
                f"Next scan_tab_diff call returns only what changed."
            ))]

        b_prev = _idx(prev.get("buttons", []), lambda b: b.get("selector"))
        b_curr = _idx(data.get("buttons", []), lambda b: b.get("selector"))
        i_prev = _idx(prev.get("inputs", []), lambda i: i.get("selector"))
        i_curr = _idx(data.get("inputs", []), lambda i: i.get("selector"))

        b_added = [b_curr[k] for k in b_curr.keys() - b_prev.keys()]
        b_removed = [b_prev[k] for k in b_prev.keys() - b_curr.keys()]
        b_changed = [b_curr[k] for k in b_curr.keys() & b_prev.keys() if b_curr[k].get("text") != b_prev[k].get("text")]

        i_added = [i_curr[k] for k in i_curr.keys() - i_prev.keys()]
        i_removed = [i_prev[k] for k in i_prev.keys() - i_curr.keys()]
        i_changed = [i_curr[k] for k in i_curr.keys() & i_prev.keys() if i_curr[k].get("value") != i_prev[k].get("value")]

        url_changed = prev.get("url") != data.get("url")
        if not any([b_added, b_removed, b_changed, i_added, i_removed, i_changed, url_changed]):
            return [TextContent(type="text", text="## Diff scan — no changes since last scan.")]

        lines = ["## Diff scan"]
        if url_changed:
            lines.append(f"**URL changed:** {prev.get('url')} → {data.get('url')}")
        for label, items in [("➕ Buttons added", b_added), ("➖ Buttons removed", b_removed), ("✏️ Buttons changed", b_changed)]:
            if items:
                lines.append(f"\n**{label}** ({len(items)}):")
                for b in items[:20]:
                    lines.append(f"  - [{b.get('tag')}] {b.get('text','')[:60]} → `{b.get('selector')}`")
        for label, items in [("➕ Inputs added", i_added), ("➖ Inputs removed", i_removed), ("✏️ Inputs changed", i_changed)]:
            if items:
                lines.append(f"\n**{label}** ({len(items)}):")
                for i in items[:20]:
                    lines.append(f"  - {i.get('label') or i.get('name')} (type={i.get('type')}) → `{i.get('selector')}`")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "new_tab":
        url = arguments.get("url", "about:blank")
        result = await cdp_browser_send(port, "Target.createTarget", {"url": url})
        target_id = result.get("targetId", "")
        return [TextContent(type="text", text=f"Opened tab: {target_id} — {url}")]

    if name == "scan_tab":
        mode = arguments.get("mode", "ax")
        if mode == "ax":
            r = await eval_in_tab(ws_url, SCAN_AX_JS)
            try:
                data = json.loads(r.get("result", {}).get("value", "{}"))
            except Exception:
                return [TextContent(type="text", text="scan_tab (ax): scan failed — page may still be loading.")]
            url = data.get("url", "")
            domain = domain_from_url(url)
            lines = [f"## AX Scan: {data.get('title')}", f"**URL:** {url}", ""]
            existing_pb = load_playbook().get(domain, {})
            if existing_pb:
                pb_lines = [f"### Playbook for {domain}"]
                if existing_pb.get("notes"):
                    for n in existing_pb["notes"][:5]:
                        pb_lines.append(f"  • {n}")
                if existing_pb.get("default_strategy"):
                    pb_lines.append(f"  Default click strategy: {existing_pb['default_strategy']}")
                lines = pb_lines + ["", "---", ""] + lines
            lines.append(f"### Interactive elements ({data.get('ref_count', 0)})")
            lines.append("Refs are stable for THIS scan. Pass them as `selector` to click/fill/key_type/upload (e.g. selector=\"@e5\"). Re-scan if the page changes.")
            lines.append("")
            for ln in data.get("lines", []):
                lines.append("  " + ln)
            return [TextContent(type="text", text="\n".join(lines))]
        result = await eval_in_tab(ws_url, SCAN_JS)
        try:
            data = json.loads(result.get("result", {}).get("value", "{}"))
        except Exception:
            return [TextContent(type="text", text="Scan failed — page may still be loading. Try again.")]

        url = data.get("url", "")
        domain = domain_from_url(url)

        lines = [f"## Page Scan: {data.get('title')}", f"**URL:** {url}", ""]

        # Prepend existing playbook knowledge for this domain
        existing_pb = load_playbook().get(domain, {})
        if existing_pb:
            pb_lines = [f"### 📖 Playbook — what we know about {domain}"]
            if existing_pb.get("notes"):
                pb_lines.append("**Notes:**")
                for n in existing_pb["notes"]:
                    pb_lines.append(f"  ⚠️  {n}")
            if existing_pb.get("click_log"):
                worked = {k: v["strategy"] for k, v in existing_pb["click_log"].items() if v.get("worked")}
                failed = [k for k, v in existing_pb["click_log"].items() if not v.get("worked")]
                if worked:
                    pb_lines.append("**Working click strategies:**")
                    for el, strategy in worked.items():
                        pb_lines.append(f"  ✅ '{el}' → use {strategy}")
                if failed:
                    pb_lines.append(f"**Known hard elements:** {', '.join(failed)}")
            if existing_pb.get("quirks"):
                pb_lines.append(f"**Quirks:** {existing_pb['quirks']}")
            if existing_pb.get("login_fields"):
                pb_lines.append(f"**Login fields:** {existing_pb['login_fields']}")
            lines = pb_lines + ["", "---", ""] + lines

        if data.get("inputs"):
            lines.append("### Form Fields")
            for inp in data["inputs"]:
                req = " *(required)*" if inp.get("required") else ""
                lines.append(f"- **{inp['label'] or inp['name']}** — type=`{inp['type']}` selector=`{inp['selector']}`{req}")
            lines.append("")

        if data.get("forms"):
            lines.append("### Forms")
            for f in data["forms"]:
                lines.append(f"- form `{f.get('id','(no id)')}` → action=`{f.get('action','')}` method=`{f.get('method','')}`  fields: {f.get('fields')}")
            lines.append("")

        if data.get("buttons"):
            lines.append("### Buttons & Links")
            for b in data["buttons"][:30]:
                lines.append(f"- [{b['tag']}] **{b['text']}** → selector=`{b['selector']}`" + (f" href={b['href']}" if b.get("href") else ""))
            lines.append("")

        if arguments.get("save_to_playbook", True) and domain and data.get("inputs"):
            playbook = _load_live_playbook_raw()  # write to live only, not merged
            if domain not in playbook:
                playbook[domain] = {}
            playbook[domain]["last_scan"] = {
                "url": url,
                "title": data.get("title"),
                "inputs": [{k: v for k, v in i.items() if k != "value"} for i in data.get("inputs", [])],
                "buttons": [{"text": b["text"], "selector": b["selector"]} for b in data.get("buttons", [])[:20]],
            }
            save_playbook_data(playbook)
            lines.append(f"*Saved to playbook for {domain}*")

        return [TextContent(type="text", text="\n".join(lines))]

    if name == "key_type":
        text = arguments["text"]
        delay = max(0, int(arguments.get("delay_ms", 30))) / 1000.0
        mode = arguments.get("mode", "keystrokes")  # "keystrokes" | "insertText" | "fast"

        if mode == "fast":
            # Fastest path: set value on focused input via React-aware setter
            # and fire input+change once. No per-char dispatch.
            # Only works when an input is currently focused.
            js = """(function(value) {
                const el = document.activeElement;
                if (!el || !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) {
                    return JSON.stringify({ok:false, error:'no input element is focused'});
                }
                const proto = (el instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input',  {bubbles:true, composed:true}));
                el.dispatchEvent(new Event('change', {bubbles:true, composed:true}));
                return JSON.stringify({ok:true, len: value.length, readback: el.value.length});
            })(""" + json.dumps(text) + ")"
            r = await eval_in_tab(ws_url, js)
            val = r.get("result", {}).get("value", "{}")
            # Record success to playbook (fast mode wins on a focused input)
            try:
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                active_sel_r = await eval_in_tab(ws_url, "document.activeElement && (document.activeElement.id || document.activeElement.name || document.activeElement.tagName) || ''")
                active_id = active_sel_r.get("result", {}).get("value", "")
                if tab_dom and active_id and '"ok":true' in val:
                    _playbook_record(tab_dom, f"key_type:{active_id}", "fast_setter", True,
                                     kind="key_type", selector_pattern=f"#{active_id}" if active_id else None)
            except Exception:
                pass
            return [TextContent(type="text", text=f"key_type (fast): {val}")]


        # Map common chars to CDP code + key (for keys that aren't single letters/digits)
        def _key_code_for(ch: str):
            specials = {
                " ": ("Space", " "),
                "\n": ("Enter", "Enter"),
                "\t": ("Tab", "Tab"),
                ".": ("Period", "."),
                ",": ("Comma", ","),
                ";": ("Semicolon", ";"),
                "/": ("Slash", "/"),
                "\\": ("Backslash", "\\"),
                "-": ("Minus", "-"),
                "=": ("Equal", "="),
                "[": ("BracketLeft", "["),
                "]": ("BracketRight", "]"),
                "'": ("Quote", "'"),
                "`": ("Backquote", "`"),
            }
            if ch in specials:
                return specials[ch]
            if ch.isalpha():
                return ("Key" + ch.upper(), ch)
            if ch.isdigit():
                return ("Digit" + ch, ch)
            # Fallback — no specific code, Chrome will still process via text field
            return ("", ch)

        if mode == "insertText":
            for ch in text:
                await cdp_send(ws_url, "Input.insertText", {"text": ch})
                if delay:
                    await asyncio.sleep(delay)
            try:
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                active_id_r = await eval_in_tab(ws_url, "document.activeElement && (document.activeElement.id || document.activeElement.name) || ''")
                active_id = active_id_r.get("result", {}).get("value", "")
                if tab_dom and active_id:
                    _playbook_record(tab_dom, f"key_type:{active_id}", "insertText", True,
                                     kind="key_type", selector_pattern=f"#{active_id}" if active_id else None)
            except Exception:
                pass
            return [TextContent(type="text", text=f"Typed {len(text)} char(s) (insertText mode).")]

        # keystrokes mode: per-char keyDown (with text= field triggers keypress+input
        # in the renderer) → keyUp. Matches what real OS keyboard drivers generate.
        # This is what Playwright does internally — fires React onChange properly.
        for ch in text:
            code, key = _key_code_for(ch)
            vk = ord(ch.upper()) if ch.isalnum() else ord(ch)
            base = {
                "key": key,
                "code": code,
                "text": ch,
                "unmodifiedText": ch,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "location": 0,
                "modifiers": 1 if ch.isupper() else 0,  # Shift modifier for capitals
            }
            await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyDown", **base})
            await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in base.items() if k != "text" and k != "unmodifiedText"}})
            if delay:
                await asyncio.sleep(delay)
        try:
            tab_url_r = await eval_in_tab(ws_url, "location.href")
            tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
            active_id_r = await eval_in_tab(ws_url, "document.activeElement && (document.activeElement.id || document.activeElement.name) || ''")
            active_id = active_id_r.get("result", {}).get("value", "")
            if tab_dom and active_id:
                _playbook_record(tab_dom, f"key_type:{active_id}", "keystrokes", True,
                                 kind="key_type", selector_pattern=f"#{active_id}" if active_id else None)
        except Exception:
            pass
        return [TextContent(type="text", text=f"Typed {len(text)} char(s) (keystrokes mode — keyDown+keyUp per char with full metadata).")]

    if name in ("react_inspect_store", "redux_dispatch"):
        from_sel = arguments.get("from_selector", "")
        max_state = int(arguments.get("max_state_chars", 4000))
        action_obj = arguments.get("action") if name == "redux_dispatch" else None
        if name == "redux_dispatch" and (not action_obj or "type" not in action_obj):
            return [TextContent(type="text", text="redux_dispatch: action must be an object with a 'type' string.")]

        # Discovery script — caches the found store on window.__chromeMcpStore__
        # so a subsequent dispatch call can reuse it without re-walking.
        js = """(function(fromSel, doDispatch, action, maxChars) {
            const findFiberKey = (node) => {
                for (const k of Object.keys(node)) {
                    if (k.startsWith('__reactFiber')           // React 17+
                     || k.startsWith('__reactInternalInstance') // React 16
                     || k.startsWith('__reactProps')) return k;
                }
                return null;
            };
            const storeFromFiber = (start) => {
                let fiber = start;
                let hops = 0;
                while (fiber && hops < 200) {
                    const props = fiber.memoizedProps || fiber.pendingProps;
                    if (props && props.store && typeof props.store.dispatch === 'function' && typeof props.store.getState === 'function') {
                        return { store: props.store, source: 'fiber:' + (fiber.type && (fiber.type.displayName || fiber.type.name) || '?'), hops };
                    }
                    // Some setups stash store on stateNode
                    if (fiber.stateNode && fiber.stateNode.store
                        && typeof fiber.stateNode.store.dispatch === 'function'
                        && typeof fiber.stateNode.store.getState === 'function') {
                        return { store: fiber.stateNode.store, source: 'stateNode', hops };
                    }
                    fiber = fiber.return;
                    hops++;
                }
                return null;
            };
            const walkAllFibers = () => {
                // Hook into React DevTools registry to find every fiber root
                const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
                if (!hook || !hook.renderers) return null;
                for (const [, renderer] of hook.renderers) {
                    if (!renderer || !renderer.findFiberByHostInstance) continue;
                    // Walk every root container
                    try {
                        const roots = (renderer.getFiberRoots && renderer.getFiberRoots(renderer)) || [];
                        for (const root of roots) {
                            const found = storeFromFiber(root.current);
                            if (found) return found;
                            // Walk children recursively (BFS)
                            const stack = [root.current];
                            let n = 0;
                            while (stack.length && n < 5000) {
                                const f = stack.pop();
                                n++;
                                const found2 = storeFromFiber(f);
                                if (found2) return found2;
                                if (f.child) stack.push(f.child);
                                if (f.sibling) stack.push(f.sibling);
                            }
                        }
                    } catch(e) {}
                }
                return null;
            };

            const discovery = { method: null, source: null, hops: null };
            let store = null;

            // 1. Cached from previous call
            if (window.__chromeMcpStore__ && window.__chromeMcpStore__.dispatch && window.__chromeMcpStore__.getState) {
                store = window.__chromeMcpStore__;
                discovery.method = 'cached';
            }

            // 2. Global window.store / window.__store__
            if (!store) {
                for (const k of ['store', '__store__', '__REDUX_STORE__', '__reduxStore']) {
                    const s = window[k];
                    if (s && typeof s.dispatch === 'function' && typeof s.getState === 'function') {
                        store = s;
                        discovery.method = 'window.' + k;
                        break;
                    }
                }
            }

            // 3. Fiber walk from selector (or body)
            if (!store) {
                const root = fromSel ? document.querySelector(fromSel) : document.body;
                if (root) {
                    const fk = findFiberKey(root);
                    if (fk) {
                        const found = storeFromFiber(root[fk]);
                        if (found) {
                            store = found.store;
                            discovery.method = 'fiber_walk_from_selector';
                            discovery.source = found.source;
                            discovery.hops = found.hops;
                        }
                    }
                }
            }

            // 4. Full React DevTools tree walk
            if (!store) {
                const found = walkAllFibers();
                if (found) {
                    store = found.store;
                    discovery.method = 'react_devtools_full_walk';
                    discovery.source = found.source;
                    discovery.hops = found.hops;
                }
            }

            // 5. Find React roots directly (no devtools hook needed).
            //    Works on production sites that strip __REACT_DEVTOOLS_GLOBAL_HOOK__.
            //    D2D fits this pattern — React 16 in production, no devtools.
            if (!store) {
                const allNodes = document.querySelectorAll('div, body, html, main, section');
                for (const node of allNodes) {
                    let rootFiber = null;
                    for (const k of Object.keys(node)) {
                        if (k.startsWith('__reactContainer')) {
                            // React 18 root container
                            rootFiber = node[k] && node[k].stateNode && node[k].stateNode.current;
                            break;
                        }
                        if (k === '_reactRootContainer') {
                            // React 16/17 legacy root
                            rootFiber = node[k]._internalRoot && node[k]._internalRoot.current;
                            break;
                        }
                    }
                    if (!rootFiber) continue;
                    // BFS from this root to find any fiber carrying a store
                    const stack = [rootFiber];
                    let n = 0;
                    while (stack.length && n < 5000) {
                        const f = stack.pop();
                        n++;
                        const found = storeFromFiber(f);
                        if (found) {
                            store = found.store;
                            discovery.method = 'react_root_direct';
                            discovery.source = found.source;
                            discovery.hops = found.hops;
                            break;
                        }
                        if (f.child) stack.push(f.child);
                        if (f.sibling) stack.push(f.sibling);
                    }
                    if (store) break;
                }
            }

            // 6. Backbone fallback — D2D's actual state container.
            //    Backbone Models/Collections expose `set(key, value)` + `save()`/`get(key)`.
            //    Not a Redux store, but we can present a uniform dispatch-like shape
            //    that calls .set() / .save() instead.
            if (!store && (window.Backbone || window.B)) {
                const Backbone = window.Backbone || window.B;
                // Find any model in the page that has the form fields we'd want
                // Best heuristic: look at every property of every React fiber for
                // an instance with both .get and .set methods (Backbone.Model interface).
                const candidates = [];
                if (Backbone.Model && Backbone.Model.prototype) {
                    // Walk window for any model instance
                    for (const k of Object.keys(window)) {
                        try {
                            const v = window[k];
                            if (v && typeof v.get === 'function' && typeof v.set === 'function' && typeof v.save === 'function') {
                                candidates.push({key: k, model: v});
                            }
                        } catch(e) {}
                    }
                }
                if (candidates.length > 0) {
                    // Wrap in a dispatch-compatible shape
                    const m = candidates[0].model;
                    store = {
                        dispatch: (a) => {
                            if (a && a.type === 'SET' && a.payload) {
                                m.set(a.payload);
                                return { committed: m.attributes };
                            }
                            if (a && a.type === 'SAVE') {
                                m.save();
                                return { saved: true };
                            }
                            throw new Error('Backbone bridge: action.type must be SET or SAVE');
                        },
                        getState: () => m.attributes || m.toJSON(),
                        __isBackboneBridge: true,
                    };
                    discovery.method = 'backbone_bridge';
                    discovery.source = candidates[0].key;
                }
            }

            const devtools = !!window.__REDUX_DEVTOOLS_EXTENSION__;
            const hookPresent = !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
            const backbonePresent = !!(window.Backbone || window.B);

            if (!store) {
                return JSON.stringify({
                    ok: false,
                    error: 'no store found',
                    redux_devtools_extension: devtools,
                    react_devtools_hook: hookPresent,
                    backbone_present: backbonePresent,
                    tried: ['cached', 'window.store/__store__/__REDUX_STORE__', 'fiber_walk_from_' + (fromSel || 'body'), 'react_devtools_full_walk', 'react_root_direct', 'backbone_bridge']
                });
            }

            // Cache for subsequent dispatch
            window.__chromeMcpStore__ = store;

            const stateBefore = JSON.stringify(store.getState());
            let stateAfter = stateBefore;
            let dispatchResult = null;
            if (doDispatch) {
                try {
                    dispatchResult = store.dispatch(action);
                    stateAfter = JSON.stringify(store.getState());
                } catch (e) {
                    return JSON.stringify({
                        ok: false,
                        discovery,
                        redux_devtools_extension: devtools,
                        error: 'dispatch threw: ' + (e && e.message)
                    });
                }
            }
            const truncate = (s) => s.length > maxChars ? s.slice(0, maxChars) + '…(truncated)' : s;

            return JSON.stringify({
                ok: true,
                discovery,
                redux_devtools_extension: devtools,
                react_devtools_hook: hookPresent,
                state_keys: (() => { try { return Object.keys(store.getState() || {}); } catch (e) { return []; } })(),
                state_before: truncate(stateBefore),
                state_after:  doDispatch ? truncate(stateAfter) : null,
                state_changed: doDispatch ? (stateBefore !== stateAfter) : null,
                dispatch_result: doDispatch ? (dispatchResult == null ? 'undefined' : String(dispatchResult).slice(0, 200)) : null
            });
        })(""" + json.dumps(from_sel) + ", " + ("true" if action_obj else "false") + ", " + (json.dumps(action_obj) if action_obj else "null") + ", " + str(max_state) + ")"
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        label = "redux_dispatch" if action_obj else "react_inspect_store"
        # Record success — finding the store + dispatching counts as a workaround strategy
        try:
            parsed = json.loads(val)
            if parsed.get("ok") and action_obj:
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                method = parsed.get("discovery", {}).get("method", "redux")
                action_type = (action_obj or {}).get("type", "?")
                if tab_dom:
                    _playbook_record(tab_dom, f"redux_dispatch:{action_type}", method, True, kind="state_dispatch")
        except Exception:
            pass
        return [TextContent(type="text", text=f"{label}: {val}")]

    if name == "aui_dispatch":
        event = arguments.get("event", "")
        target_sel = arguments.get("target_selector", "")
        payload = arguments.get("payload")
        js = """(function(event, targetSel, payload) {
            const result = { ok: false };
            const A = window.A;
            if (!A) {
                result.error = 'window.A (Amazon AUI) not present on this page';
                return JSON.stringify(result);
            }
            result.aui_present = true;
            result.aui_version = A.version || null;
            result.has_declarative = !!(A.declarative && A.declarative.fire);
            result.state_stores = (A.state && typeof A.state === 'object') ? Object.keys(A.state) : [];
            // data-action handler inventory
            const actionScope = targetSel ? document.querySelector(targetSel) : document;
            const actionEls = (actionScope || document).querySelectorAll('[data-action]');
            const actions = {};
            actionEls.forEach(el => {
                const a = el.getAttribute('data-action');
                actions[a] = (actions[a] || 0) + 1;
            });
            result.data_actions = actions;
            // Module loader presence
            result.has_P = !!window.P;
            result.P_modules = (window.P && window.P.modules) ? Object.keys(window.P.modules).slice(0, 30) : [];

            if (!event) {
                result.ok = true;
                result.mode = 'inspect';
                return JSON.stringify(result);
            }
            // Fire mode
            if (!targetSel) {
                result.error = 'target_selector required when event is set';
                return JSON.stringify(result);
            }
            const target = document.querySelector(targetSel);
            if (!target) {
                result.error = 'target_selector did not match: ' + targetSel;
                return JSON.stringify(result);
            }
            try {
                if (A.declarative && A.declarative.fire) {
                    A.declarative.fire(event, target, payload);
                    result.ok = true;
                    result.mode = 'fire_declarative';
                    return JSON.stringify(result);
                }
                if (A.$ && A.$(target).trigger) {
                    A.$(target).trigger(event, payload);
                    result.ok = true;
                    result.mode = 'fire_AdollarTrigger';
                    return JSON.stringify(result);
                }
                result.error = 'no known AUI dispatch primitive (no A.declarative.fire, no A.$().trigger)';
                return JSON.stringify(result);
            } catch (e) {
                result.error = 'dispatch threw: ' + (e && e.message);
                return JSON.stringify(result);
            }
        })(""" + json.dumps(event) + ", " + json.dumps(target_sel) + ", " + json.dumps(payload) + ")"
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        try:
            parsed = json.loads(val)
            if parsed.get("ok"):
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                if tab_dom:
                    _playbook_record(tab_dom, f"aui_dispatch:{event}", "aui_dispatch", True, kind="state_dispatch")
        except Exception:
            pass
        return [TextContent(type="text", text=f"aui_dispatch: {val}")]

    if name == "backbone_inspect":
        max_chars = int(arguments.get("max_chars", 4000))
        js = """(function(maxChars) {
            const result = { ok: false };
            const B = window.Backbone;
            if (!B) {
                result.error = 'window.Backbone not present';
                return JSON.stringify(result);
            }
            result.ok = true;
            result.backbone_version = B.VERSION || null;
            // history
            try {
                if (B.history) {
                    result.history_started = !!B.history._hasPushState || !!B.history._wantsPushState;
                    result.handlers = (B.history.handlers || []).map(h => ({
                        route: h.route ? h.route.source : String(h.route),
                    })).slice(0, 40);
                    result.fragment = B.history.fragment || null;
                }
            } catch(e) { result.history_error = String(e); }
            // Models/Collections registered on common globals
            const knownGlobals = ['App', 'app', 'window.app', 'KDP', 'kdp', 'AppState'];
            result.likely_app_namespaces = knownGlobals.filter(n => {
                try { return !!eval(n); } catch(e) { return false; }
            });
            // Look for any model instances on window
            const winKeys = Object.keys(window);
            const candidates = [];
            for (const k of winKeys) {
                try {
                    const v = window[k];
                    if (v && (v instanceof B.Model || v instanceof B.Collection || v instanceof B.View || v instanceof B.Router)) {
                        candidates.push({key: k, type: v.constructor && v.constructor.name || 'unknown'});
                        if (candidates.length >= 20) break;
                    }
                } catch(e) {}
            }
            result.backbone_instances_on_window = candidates;
            const s = JSON.stringify(result);
            return s.length > maxChars ? s.slice(0, maxChars) + '...(truncated)' : s;
        })(""" + str(max_chars) + ")"
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        return [TextContent(type="text", text=f"backbone_inspect: {val}")]

    if name == "lexical_set_text":
        container_sel = arguments["container_selector"]
        text_to_set = arguments["text"]
        click_ph = arguments.get("click_placeholder_selector", "") or ""
        mount_timeout = float(arguments.get("mount_timeout_seconds", 5) or 5)
        submit_enter = bool(arguments.get("submit_via_enter", False))

        js = """(async function(containerSel, text, clickPh, mountTimeout, submitEnter) {
            const result = { ok: false, steps: [] };
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));

            // 1. Optionally click placeholder to mount the editor, then poll for contenteditable
            if (clickPh) {
                const ph = document.querySelector(clickPh);
                if (ph) {
                    ph.scrollIntoView({block:'center'});
                    // Fire full mouse sequence — Lexical placeholders sometimes listen for mousedown
                    const fire = (t) => new MouseEvent(t, {bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window});
                    ph.dispatchEvent(fire('mousedown'));
                    ph.dispatchEvent(fire('mouseup'));
                    ph.click();
                    result.steps.push('placeholder clicked');
                } else {
                    result.steps.push('placeholder selector not found, continuing');
                }
                // Poll for the contenteditable to mount
                const deadline = Date.now() + mountTimeout * 1000;
                while (Date.now() < deadline) {
                    const probe = document.querySelector(containerSel);
                    if (probe && (probe.isContentEditable || probe.querySelector('[contenteditable=true]'))) break;
                    await sleep(120);
                }
            }

            // 2. Locate the actual contenteditable
            let editable = document.querySelector(containerSel);
            if (editable && !editable.isContentEditable) {
                editable = editable.querySelector('[contenteditable=true]') || editable.querySelector('[contenteditable]') || editable;
            }
            if (!editable) {
                result.error = 'no contenteditable found at or under: ' + containerSel;
                return JSON.stringify(result);
            }
            result.steps.push('editable located: ' + editable.tagName + (editable.id ? '#'+editable.id : ''));

            // 3. Find Lexical's editor instance — exposed as __lexicalEditor on the root contenteditable
            //    (or on a parent — Lexical sometimes registers it via setRootElement)
            let lexEditor = null;
            let cur = editable;
            for (let i = 0; i < 6 && cur; i++) {
                if (cur.__lexicalEditor) { lexEditor = cur.__lexicalEditor; break; }
                cur = cur.parentElement;
            }
            // Also scan children — some Lexical configs nest the root one level down
            if (!lexEditor) {
                const all = editable.querySelectorAll('*');
                for (let i = 0; i < Math.min(20, all.length); i++) {
                    if (all[i].__lexicalEditor) { lexEditor = all[i].__lexicalEditor; break; }
                }
            }

            editable.focus();

            // 4. STRATEGY A — Direct Lexical API via setEditorState with a serialized clean state
            if (lexEditor && typeof lexEditor.setEditorState === 'function' && typeof lexEditor.parseEditorState === 'function') {
                try {
                    // Build clean Lexical state JSON. Schema is stable across recent versions (1.x, 2.x).
                    // We split on newlines into paragraphs so multi-line text renders correctly.
                    const lines = text.split(/\\r?\\n/);
                    const paragraphs = lines.map(line => {
                        const children = line.length
                            ? [{detail:0, format:0, mode:'normal', style:'', text:line, type:'text', version:1}]
                            : [];
                        return {children, direction:'ltr', format:'', indent:0, type:'paragraph', version:1};
                    });
                    const stateJson = {
                        root: {children: paragraphs, direction:'ltr', format:'', indent:0, type:'root', version:1}
                    };
                    const parsed = lexEditor.parseEditorState(JSON.stringify(stateJson));
                    lexEditor.setEditorState(parsed);
                    // Force a sync render
                    if (typeof lexEditor.focus === 'function') lexEditor.focus();
                    await sleep(120);
                    result.steps.push('lexical-api setEditorState succeeded');
                    result.mode = 'lexical-api';
                    result.ok = true;
                    result.readback_len = (editable.innerText || '').length;
                    result.sample = (editable.innerText || '').slice(0, 120);
                    if (submitEnter) {
                        editable.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter', keyCode:13}));
                    }
                    return JSON.stringify(result);
                } catch (e) {
                    result.steps.push('lexical-api failed: ' + (e && e.message));
                    // fall through to events
                }
            } else {
                result.steps.push('no __lexicalEditor accessible — using event path');
            }

            // 5. STRATEGY B — Pure event dispatch path (no direct Lexical access)
            //    Select-all → delete via beforeinput → paste via ClipboardEvent with DataTransfer.
            try {
                // Select all contents
                const range = document.createRange();
                range.selectNodeContents(editable);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);

                // Delete via beforeinput — Lexical listens to this
                const delEv = new InputEvent('beforeinput', {
                    bubbles: true, cancelable: true, composed: true,
                    inputType: 'deleteContent',
                });
                editable.dispatchEvent(delEv);
                // Also dispatch the native execCommand as a belt-and-braces
                try { document.execCommand('selectAll', false); document.execCommand('delete', false); } catch(e){}
                await sleep(60);

                // Paste via ClipboardEvent with proper DataTransfer
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const pasteEv = new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, composed: true,
                    clipboardData: dt,
                });
                editable.dispatchEvent(pasteEv);
                // For editors that DON'T listen to ClipboardEvent — fire a beforeinput insertFromPaste
                const insEv = new InputEvent('beforeinput', {
                    bubbles: true, cancelable: true, composed: true,
                    inputType: 'insertFromPaste',
                    data: text,
                });
                editable.dispatchEvent(insEv);
                await sleep(180);

                if (submitEnter) {
                    editable.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter', keyCode:13}));
                }

                result.steps.push('event-path dispatched');
                result.mode = 'events-only';
                result.ok = true;
                result.readback_len = (editable.innerText || '').length;
                result.sample = (editable.innerText || '').slice(0, 120);
                return JSON.stringify(result);
            } catch (e) {
                result.error = 'event-path threw: ' + (e && e.message);
                return JSON.stringify(result);
            }
        })(""" + json.dumps(container_sel) + ", " + json.dumps(text_to_set) + ", " + json.dumps(click_ph) + ", " + str(mount_timeout) + ", " + ("true" if submit_enter else "false") + ")"
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        try:
            parsed = json.loads(val)
            if parsed.get("ok"):
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                mode = parsed.get("mode", "lexical")
                if tab_dom:
                    _playbook_record(tab_dom, f"lexical_set_text:{arguments.get('container_selector', '?')}",
                                     mode, True, kind="fill",
                                     selector_pattern=arguments.get('container_selector'))
        except Exception:
            pass
        return [TextContent(type="text", text=f"lexical_set_text: {val}")]

    if name == "draftjs_set_text":
        # Draft.js requires a totally different strategy than Lexical:
        # - handlePastedText blocks synthetic paste events (isTrusted check)
        # - EditorState is in React fiber props, not on a DOM property
        # - Real CDP keystrokes go through Draft's keydown handlers and DO update state
        container_sel = arguments["container_selector"]
        text_to_set = arguments["text"]
        click_first = bool(arguments.get("click_first", True))
        submit_enter = bool(arguments.get("submit_via_enter", False))
        delay_ms = int(arguments.get("delay_ms", 80))
        verify_per_char = bool(arguments.get("verify_per_char", False))
        # Clamp delay to a sane range
        delay_ms = max(20, min(500, delay_ms))
        delay_s = delay_ms / 1000.0

        # STRATEGY 1+2 (in-page JS): fiber-walk to find Draft editor, then beforeinput
        js_strat12 = """(async function(containerSel, text) {
            const result = {steps: []};
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            let editable = document.querySelector(containerSel);
            if (editable && !editable.isContentEditable) {
                editable = editable.querySelector('[contenteditable=true]') || editable.querySelector('[contenteditable]') || editable;
            }
            if (!editable) return JSON.stringify({ok:false, error:'no contenteditable at '+containerSel});
            editable.scrollIntoView({block:'center'});
            editable.focus();
            result.steps.push('editable located: ' + editable.tagName);

            // Try fiber-walk for Draft editor (props.editorState + props.onChange)
            const fiberKey = Object.keys(editable).find(k => k.startsWith('__reactFiber'));
            let draftInstance = null;
            if (fiberKey) {
                let f = editable[fiberKey];
                let hops = 0;
                while (f && hops < 40) {
                    const sn = f.stateNode;
                    if (sn && sn.props && sn.props.editorState && typeof sn.props.onChange === 'function') {
                        draftInstance = sn;
                        result.steps.push('fiber: found Draft editor at hop '+hops);
                        break;
                    }
                    if (f.memoizedProps && f.memoizedProps.editorState && typeof f.memoizedProps.onChange === 'function') {
                        draftInstance = { props: f.memoizedProps, _fromMemoized: true };
                        result.steps.push('fiber: found memoized props at hop '+hops);
                        break;
                    }
                    f = f.return;
                    hops++;
                }
            }
            if (!draftInstance) result.steps.push('fiber: no Draft editor instance reachable');

            // STRATEGY 2: chunked beforeinput insertText. Draft.js's keypress
            // handler accepts beforeinput as a real typing event, but silently
            // truncates large single dispatches (~observed cap around 30-60 chars
            // on X composer). Fix: dispatch in small chunks and verify after
            // each batch. Re-target the contenteditable each pass — Draft may
            // re-mount the editable node mid-insert.
            try {
                const CHUNK = 16;       // chars per dispatch
                const PASS_DELAY = 25;  // ms between dispatches
                const STALL_LIMIT = 3;  // give up after N no-progress passes
                let cursor = 0;
                let stalls = 0;
                let lastLen = 0;

                const findEditable = () => {
                    let el = document.querySelector(containerSel);
                    if (el && !el.isContentEditable) {
                        el = el.querySelector('[contenteditable=true]') || el.querySelector('[contenteditable]') || el;
                    }
                    return el;
                };

                while (cursor < text.length && stalls < STALL_LIMIT) {
                    const live = findEditable();
                    if (!live) break;
                    live.focus();
                    const remaining = text.slice(cursor);
                    const chunk = remaining.slice(0, CHUNK);
                    const ev = new InputEvent('beforeinput', {
                        bubbles: true, cancelable: true, composed: true,
                        inputType: 'insertText',
                        data: chunk,
                    });
                    live.dispatchEvent(ev);
                    await sleep(PASS_DELAY);
                    const curText = (live.innerText || '');
                    const curLen = curText.length;
                    if (curLen > lastLen) {
                        cursor += (curLen - lastLen);
                        lastLen = curLen;
                        stalls = 0;
                    } else {
                        stalls += 1;
                        await sleep(60);
                    }
                }
                // Final settle
                await sleep(120);
                const finalEl = findEditable();
                const finalLen = finalEl ? (finalEl.innerText || '').length : 0;
                // Accept if we got ≥95% — Draft.js can drop trailing whitespace
                if (finalLen >= Math.floor(text.length * 0.95)) {
                    result.ok = true;
                    result.mode = 'beforeinput-chunked';
                    result.readback_len = finalLen;
                    result.expected_len = text.length;
                    result.sample = (finalEl.innerText || '').slice(0, 120);
                    return JSON.stringify(result);
                }
                result.steps.push('chunked beforeinput stalled at ' + finalLen + '/' + text.length + ' (stalls=' + stalls + ')');
            } catch (e) {
                result.steps.push('chunked beforeinput threw: ' + (e && e.message));
            }
            // Signal to Python to try CDP keystroke strategy
            return JSON.stringify({ ok:false, fallback:'cdp_keystrokes', steps: result.steps });
        })(""" + json.dumps(container_sel) + ", " + json.dumps(text_to_set) + ")"

        r = await eval_in_tab(ws_url, js_strat12)
        raw = r.get("result", {}).get("value", "{}")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            parsed = {"ok": False, "raw": raw}

        # Strategies 1+2 succeeded — return
        if parsed.get("ok"):
            try:
                cur_url = await eval_in_tab(ws_url, "location.href")
                d = domain_from_url(cur_url.get("result", {}).get("value", ""))
                if d:
                    _playbook_record(d, f"draftjs_set_text:{container_sel}", parsed.get("mode", "?"), True, kind="fill", selector_pattern=container_sel)
            except Exception:
                pass
            return [TextContent(type="text", text=f"draftjs_set_text: {json.dumps(parsed)}")]

        # STRATEGY 3 — Real CDP keystrokes with focus-settle warmup ritual
        # Get element coords for the click
        coord_js = """(function(s){
            let el = document.querySelector(s);
            if (el && !el.isContentEditable) el = el.querySelector('[contenteditable=true]') || el;
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.left + r.width/2, y: r.top + Math.min(40, r.height/2), w: r.width, h: r.height};
        })(""" + json.dumps(container_sel) + ")"
        cr = await eval_in_tab(ws_url, coord_js)
        coords = cr.get("result", {}).get("value")

        if click_first and coords:
            # Real CDP click
            x, y = coords["x"], coords["y"]
            for ev_type in ("mousePressed", "mouseReleased"):
                await cdp_send(ws_url, "Input.dispatchMouseEvent", {
                    "type": ev_type, "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1,
                })
            await asyncio.sleep(0.35)  # let Draft's focus catcher hand off to real root

            # Warmup ritual: space + backspace
            for vk, txt in [(32, " "), (8, None)]:
                payload_down = {"type": "keyDown", "key": " " if vk == 32 else "Backspace",
                                "code": "Space" if vk == 32 else "Backspace",
                                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
                if txt is not None:
                    payload_down["text"] = txt
                await cdp_send(ws_url, "Input.dispatchKeyEvent", payload_down)
                await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyUp",
                    "key": " " if vk == 32 else "Backspace",
                    "code": "Space" if vk == 32 else "Backspace",
                    "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
                await asyncio.sleep(0.04)
            await asyncio.sleep(0.10)

        # Type each char via Input.dispatchKeyEvent with proper keyDown/keyUp
        # pairs + the `text` field. This is REAL TYPING — what Draft.js's
        # keypress handler expects. Earlier this handler used Input.insertText
        # (IME composition mode), which Draft.js drops or commits unpredictably
        # at non-human cadences (40ms partial drops, 80ms 100% drops because
        # composition window expires mid-flight). Real keystrokes don't have
        # that race.
        dropped = 0
        retried = 0
        readback_js = f"(function(s){{let el=document.querySelector(s); if(el && !el.isContentEditable) el = el.querySelector('[contenteditable=true]')||el; return el ? (el.innerText||'') : ''}})({json.dumps(container_sel)})"

        def _char_to_keystroke(ch: str) -> tuple[dict, dict]:
            """Map any char → (keyDown, keyUp) CDP payloads. The `text` field
            on keyDown is what makes Chrome produce the actual character —
            without it, the key fires but no text is entered."""
            if ch == " ":
                key, code, vk = " ", "Space", 32
            elif ch == "\n":
                key, code, vk = "Enter", "Enter", 13
            elif ch == "\t":
                key, code, vk = "Tab", "Tab", 9
            elif ch.isalpha():
                key, code, vk = ch, "Key" + ch.upper(), ord(ch.upper())
            elif ch.isdigit():
                key, code, vk = ch, "Digit" + ch, ord(ch)
            else:
                # Punctuation/symbols — pass char through, use ASCII code for vk
                key, code, vk = ch, "Unidentified", ord(ch) if len(ch) == 1 else 0
            down = {
                "type": "keyDown",
                "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                "text": ch, "unmodifiedText": ch,
            }
            up = {
                "type": "keyUp",
                "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            }
            return down, up

        async def _safe_readback() -> str:
            """Read current composer text. Defensive against null returns and
            transient CDP failures — never raises, always returns a string."""
            try:
                rb = await eval_in_tab(ws_url, readback_js)
                if not isinstance(rb, dict):
                    return ""
                val = rb.get("result", {}).get("value")
                if val is None:
                    return ""
                if not isinstance(val, str):
                    val = str(val)
                return val
            except Exception:
                return ""

        if verify_per_char:
            # Expected length grows by 1 per successfully-landed char
            expected = 0
            for ch in text_to_set:
                landed = False
                for attempt in range(3):
                    down, up = _char_to_keystroke(ch)
                    try:
                        await cdp_send(ws_url, "Input.dispatchKeyEvent", down)
                        await cdp_send(ws_url, "Input.dispatchKeyEvent", up)
                    except Exception:
                        # CDP hiccup — give it one more shot
                        await asyncio.sleep(0.05)
                        continue
                    await asyncio.sleep(delay_s)
                    cur = await _safe_readback()
                    if len(cur) >= expected + 1:
                        landed = True
                        expected = len(cur)
                        if attempt > 0:
                            retried += 1
                        break
                    # Brief extra settle before retry
                    await asyncio.sleep(delay_s)
                if not landed:
                    dropped += 1
        else:
            # Fast path — fixed delay between real keystrokes, no per-char verify
            for ch in text_to_set:
                down, up = _char_to_keystroke(ch)
                try:
                    await cdp_send(ws_url, "Input.dispatchKeyEvent", down)
                    await cdp_send(ws_url, "Input.dispatchKeyEvent", up)
                except Exception:
                    pass
                await asyncio.sleep(delay_s)

        await asyncio.sleep(0.20)

        # Final readback
        rb = await eval_in_tab(ws_url, readback_js)
        sample_full = rb.get("result", {}).get("value", "")
        sample = sample_full[:200]
        readback_len = len(sample_full)
        success = readback_len >= int(len(text_to_set) * 0.95)

        if submit_enter and success:
            await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"})
            await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})

        try:
            cur_url = await eval_in_tab(ws_url, "location.href")
            d = domain_from_url(cur_url.get("result", {}).get("value", ""))
            if d:
                _playbook_record(d, f"draftjs_set_text:{container_sel}", "cdp_keystrokes", success, kind="fill", selector_pattern=container_sel)
        except Exception:
            pass

        out = {
            "ok": success,
            "mode": "cdp_keystrokes",
            "readback_len": readback_len,
            "expected_len": len(text_to_set),
            "delay_ms": delay_ms,
            "dropped_chars": dropped,
            "retried_chars": retried,
            "verify_per_char": verify_per_char,
            "sample": sample[:120],
            "steps": parsed.get("steps", []) + [f"cdp keystrokes (delay={delay_ms}ms, verify={verify_per_char})"],
        }
        if not success:
            out["hint"] = "char drops detected — increase delay_ms (try 120-150) or pass verify_per_char=true"
        return [TextContent(type="text", text=f"draftjs_set_text: {json.dumps(out)}")]

    # ── #1 VISION FALLBACK ──────────────────────────────────────────────
    if name == "vision_check":
        import base64
        question = arguments["question"]
        include_coords = bool(arguments.get("include_coords", True))
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return [TextContent(type="text", text=json.dumps({
                "ok": False,
                "error": "ANTHROPIC_API_KEY not set — vision_check requires Claude vision.",
            }, indent=2))]
        # Capture viewport screenshot via CDP
        try:
            shot = await cdp_send(ws_url, "Page.captureScreenshot", {"format": "png"})
            png_b64 = shot.get("data") or shot.get("result", {}).get("data") or ""
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"screenshot failed: {e}"}, indent=2))]
        if not png_b64:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "empty screenshot"}, indent=2))]

        # Get viewport dims so the model can return absolute coords
        vp = await eval_in_tab(ws_url, "JSON.stringify({w:innerWidth, h:innerHeight, dpr:devicePixelRatio||1})")
        vp_data = {}
        try:
            vp_data = json.loads(vp.get("result", {}).get("value", "{}"))
        except Exception:
            pass

        coords_prompt = ""
        if include_coords:
            coords_prompt = (
                f"\n\nThe viewport is {vp_data.get('w','?')}x{vp_data.get('h','?')} (devicePixelRatio={vp_data.get('dpr',1)}). "
                "If the question is locating a UI element, return your answer as JSON only: "
                "{\"answer\": \"...\", \"click\": {\"x\": <int>, \"y\": <int>}}. "
                "Coordinates are in CSS pixels (viewport), origin top-left, click center of the element. "
                "If the element isn't visible or the question isn't a click target, return {\"answer\": \"...\", \"click\": null}."
            )

        import urllib.request as _req
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
                    {"type": "text", "text": f"{question}{coords_prompt}"},
                ],
            }],
        }
        try:
            req = _req.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(body).encode(),
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            with _req.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"claude call failed: {e}"}, indent=2))]
        text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text").strip()
        # Try to extract coords if model returned JSON
        coords = None
        answer_text = text
        try:
            jstart = text.find("{")
            jend = text.rfind("}")
            if jstart >= 0 and jend > jstart:
                parsed = json.loads(text[jstart:jend + 1])
                if isinstance(parsed, dict):
                    answer_text = parsed.get("answer", text)
                    if parsed.get("click") and isinstance(parsed["click"], dict):
                        coords = {"x": parsed["click"].get("x"), "y": parsed["click"].get("y")}
        except Exception:
            pass

        try:
            cur = await eval_in_tab(ws_url, "location.href")
            dom = domain_from_url(cur.get("result", {}).get("value", ""))
            if dom:
                _playbook_record(dom, f"vision_check:{question[:40]}", "claude_vision", True, kind="vision")
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps({
            "ok": True, "answer": answer_text, "click": coords,
            "tokens_in": data.get("usage", {}).get("input_tokens", 0),
            "tokens_out": data.get("usage", {}).get("output_tokens", 0),
        }, indent=2))]

    # ── companion to vision_check: click at absolute coords ────────────
    if name == "click_at_coords":
        x = float(arguments["x"])
        y = float(arguments["y"])
        double = bool(arguments.get("double", False))
        for ev_type in ("mousePressed", "mouseReleased"):
            await cdp_send(ws_url, "Input.dispatchMouseEvent", {
                "type": ev_type, "x": x, "y": y, "button": "left", "buttons": 1,
                "clickCount": 2 if double else 1,
            })
        if double:
            for ev_type in ("mousePressed", "mouseReleased"):
                await cdp_send(ws_url, "Input.dispatchMouseEvent", {
                    "type": ev_type, "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 2,
                })
        try:
            cur = await eval_in_tab(ws_url, "location.href")
            dom = domain_from_url(cur.get("result", {}).get("value", ""))
            if dom:
                _playbook_record(dom, f"click_at_coords:({x:.0f},{y:.0f})", "cdp_coords", True, kind="click")
        except Exception:
            pass
        return [TextContent(type="text", text=json.dumps({"ok": True, "x": x, "y": y, "double": double}))]

    # ── #5 STEALTH PATCHES ──────────────────────────────────────────────
    if name == "enable_stealth":
        # Register a persistent on-new-document script that masks the most-checked
        # automation tells. Real-Chrome sessions usually don't need this, but it
        # unblocks fresh-profile flows + sites that probe for CDP attachment.
        stealth_js = """(() => {
            // navigator.webdriver — the textbook tell
            try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
            // navigator.languages — many sites flag empty array
            try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] }); } catch (e) {}
            // navigator.plugins — headless reports 0 plugins
            try {
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}],
                });
            } catch (e) {}
            // chrome runtime — present in real Chrome, missing in headless
            try { if (!window.chrome) window.chrome = { runtime: {} }; } catch (e) {}
            // permissions API normalization
            try {
                const orig = navigator.permissions && navigator.permissions.query;
                if (orig) {
                    navigator.permissions.query = (p) =>
                        p.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : orig.call(navigator.permissions, p);
                }
            } catch (e) {}
            // WebGL vendor / renderer — common fingerprint vector
            try {
                const getParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p) {
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam.call(this, p);
                };
            } catch (e) {}
        })();"""
        try:
            conn = await _get_conn(ws_url)
            await conn.send("Page.enable")
            result = await conn.send("Page.addScriptToEvaluateOnNewDocument", {"source": stealth_js, "runImmediately": True})
            ident = result.get("identifier") if isinstance(result, dict) else None
            # Also apply once to current document
            await eval_in_tab(ws_url, stealth_js)
            return [TextContent(type="text", text=json.dumps({"ok": True, "identifier": ident, "applied": [
                "navigator.webdriver=undefined", "navigator.languages", "navigator.plugins",
                "window.chrome.runtime", "permissions.query notifications", "WebGL vendor",
            ]}, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, indent=2))]

    # ── #8 CAPTCHA SOLVER ───────────────────────────────────────────────
    if name == "solve_captcha":
        import urllib.request as _req
        provider = os.environ.get("CAPTCHA_PROVIDER", "").lower()
        api_key = os.environ.get("CAPTCHA_API_KEY", "")
        if not provider or not api_key:
            return [TextContent(type="text", text=json.dumps({
                "ok": False,
                "error": "CAPTCHA_PROVIDER (twocaptcha|capmonster) and CAPTCHA_API_KEY env vars required",
                "hint": "no provider configured — fall back to pause_for_human",
            }, indent=2))]
        ctype = arguments["type"]
        site_key = arguments["site_key"]
        page_url = arguments.get("page_url") or ""
        if not page_url:
            try:
                cu = await eval_in_tab(ws_url, "location.href")
                page_url = cu.get("result", {}).get("value", "")
            except Exception:
                pass
        action = arguments.get("action", "")
        min_score = arguments.get("min_score", 0.3)

        if provider == "twocaptcha":
            submit_params = {
                "key": api_key,
                "json": "1",
                "googlekey": site_key if "recaptcha" in ctype else None,
                "sitekey": site_key if ctype in ("hcaptcha", "turnstile") else None,
                "pageurl": page_url,
            }
            if ctype == "recaptcha_v2":
                submit_params["method"] = "userrecaptcha"
            elif ctype == "recaptcha_v3":
                submit_params["method"] = "userrecaptcha"
                submit_params["version"] = "v3"
                if action: submit_params["action"] = action
                submit_params["min_score"] = str(min_score)
            elif ctype == "hcaptcha":
                submit_params["method"] = "hcaptcha"
            elif ctype == "turnstile":
                submit_params["method"] = "turnstile"
            # Compact params, drop None
            submit_params = {k: v for k, v in submit_params.items() if v is not None}
            try:
                from urllib.parse import urlencode
                submit_url = "https://2captcha.com/in.php?" + urlencode(submit_params)
                with _req.urlopen(submit_url, timeout=20) as r:
                    sub = json.loads(r.read().decode())
                if sub.get("status") != 1:
                    return [TextContent(type="text", text=json.dumps({"ok": False, "error": sub}, indent=2))]
                task_id = sub.get("request")
                # Poll for result (max 120s)
                for _ in range(40):
                    await asyncio.sleep(3.0)
                    poll_url = f"https://2captcha.com/res.php?key={api_key}&action=get&id={task_id}&json=1"
                    with _req.urlopen(poll_url, timeout=20) as r:
                        res = json.loads(r.read().decode())
                    if res.get("status") == 1:
                        return [TextContent(type="text", text=json.dumps({"ok": True, "token": res.get("request"), "task_id": task_id}, indent=2))]
                    if res.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                        return [TextContent(type="text", text=json.dumps({"ok": False, "error": res}, indent=2))]
                return [TextContent(type="text", text=json.dumps({"ok": False, "error": "timeout after 120s"}, indent=2))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, indent=2))]
        else:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"provider '{provider}' not yet implemented (twocaptcha works)"}, indent=2))]

    # ── CHECK THREAD UPDATES ─────────────────────────────────────────────
    if name == "check_thread_updates":
        result = _auto_update_threads_once()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── SHOW RECENT AUTO HEALS ───────────────────────────────────────────
    if name == "show_recent_auto_heals":
        limit = int(arguments.get("limit", 10))
        notif = Path.home() / ".webloom" / "auto_updates.jsonl"
        if not notif.exists():
            return [TextContent(type="text", text=json.dumps({"events": [], "total_logged": 0, "hint": "No auto-update events yet. Engine will log here after pulling its first marketplace patch."}, indent=2))]
        events = []
        try:
            with open(notif, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
        total = len(events)
        recent = events[-limit:][::-1]
        return [TextContent(type="text", text=json.dumps({"events": recent, "total_logged": total}, indent=2))]

    # ── REACT_INVOKE_HANDLER — bypass overlay event interception ───────
    if name == "react_invoke_handler":
        sel = arguments["selector"]
        handler = arguments.get("handler", "onClick")
        extra = arguments.get("event_payload", {}) or {}
        # If handler is "auto", try a sequence of common handler names
        # in order. Stops at the first that returns ok:true.
        auto_handler = handler == "auto"
        handler_sequence = ["onClick", "onPointerDown", "onMouseDown", "onSubmit"] if auto_handler else [handler]

        # Iterate through handler_sequence — first one that lands wins
        attempts = []
        val = "{}"
        for h in handler_sequence:
            js = f"""(function(sel, handlerName, extra) {{
            // Shadow-DOM-aware deep query: walks every open shadowRoot in the document
            // until it finds a match. Required for LinkedIn / Lit-based sites that wrap
            // composer modals inside <interop-outlet>.shadowRoot.
            function deepQuery(root, s) {{
                const direct = root.querySelector(s);
                if (direct) return direct;
                const all = root.querySelectorAll('*');
                for (const node of all) {{
                    if (node.shadowRoot) {{
                        const inner = deepQuery(node.shadowRoot, s);
                        if (inner) return inner;
                    }}
                }}
                return null;
            }}
            const el = deepQuery(document, sel);
            if (!el) return JSON.stringify({{ok: false, error: 'no element matched (light + shadow): ' + sel}});

            // Build a synthetic event object — duck-typed React event
            const ev = Object.assign({{
                preventDefault: function() {{ this.defaultPrevented = true; }},
                stopPropagation: function() {{}},
                stopImmediatePropagation: function() {{}},
                nativeEvent: {{}},
                bubbles: true,
                cancelable: true,
                currentTarget: el,
                target: el,
                type: handlerName.replace(/^on/, '').toLowerCase(),
                defaultPrevented: false,
                isTrusted: false,
            }}, extra || {{}});

            // 1) Direct check: __reactProps$ on the element itself
            const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps'));
            if (propsKey && el[propsKey] && typeof el[propsKey][handlerName] === 'function') {{
                try {{
                    el[propsKey][handlerName](ev);
                    return JSON.stringify({{ok: true, via: 'reactProps', hops: 0, handler: handlerName}});
                }} catch (e) {{
                    return JSON.stringify({{ok: false, error: 'handler threw: ' + (e && e.message), via: 'reactProps'}});
                }}
            }}

            // 2) Walk the fiber chain looking for the handler in memoizedProps / pendingProps
            const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
            if (!fiberKey) return JSON.stringify({{ok: false, error: 'no react fiber on this element — is the site really React?'}});
            let fiber = el[fiberKey];
            let hops = 0;
            while (fiber && hops < 20) {{
                const props = fiber.memoizedProps || fiber.pendingProps;
                if (props && typeof props[handlerName] === 'function') {{
                    try {{
                        props[handlerName](ev);
                        return JSON.stringify({{ok: true, via: 'fiber.memoizedProps', hops: hops, handler: handlerName}});
                    }} catch (e) {{
                        return JSON.stringify({{ok: false, error: 'handler threw at hop ' + hops + ': ' + (e && e.message), via: 'fiber'}});
                    }}
                }}
                fiber = fiber.return;
                hops++;
            }}

            return JSON.stringify({{ok: false, error: 'no ' + handlerName + ' found in fiber chain', searched_hops: hops}});
        }})({json.dumps(sel)}, {json.dumps(h)}, {json.dumps(extra)})"""

            r = await eval_in_tab(ws_url, js)
            val = r.get("result", {}).get("value", "{}")
            try:
                parsed = json.loads(val) if isinstance(val, str) else {}
            except Exception:
                parsed = {}
            attempts.append({"handler": h, "ok": bool(parsed.get("ok")), "via": parsed.get("via"), "error": parsed.get("error")})

            # Record this attempt in the playbook (each attempt counts)
            try:
                cur = await eval_in_tab(ws_url, "location.href")
                dom = domain_from_url(cur.get("result", {}).get("value", ""))
                if dom:
                    _playbook_record(dom, f"react_invoke_handler:{sel} via {h}",
                                     "react_fiber_handler", bool(parsed.get("ok")), kind="click",
                                     selector_pattern=sel)
            except Exception:
                pass

            if parsed.get("ok"):
                # Wrap success — include the attempt log so callers can see which handler won
                wrapped = {**parsed, "tried_handlers": [a["handler"] for a in attempts]}
                return [TextContent(type="text", text=f"react_invoke_handler: {json.dumps(wrapped)}")]

        # All handlers in the sequence failed — return the final attempt with the trace
        final = {
            "ok": False,
            "error": "all handlers failed" if auto_handler else attempts[-1].get("error") if attempts else "no attempts",
            "tried_handlers": [a["handler"] for a in attempts],
            "attempts": attempts,
        }
        return [TextContent(type="text", text=f"react_invoke_handler: {json.dumps(final)}")]

    # ── SWARM_RUN — 2-role multi-agent (Driver + Watcher) ──────────────
    if name == "swarm_run":
        import base64, time as _t
        goal = arguments["goal"]
        thread_domain = arguments.get("thread_domain") or ""
        max_minutes = int(arguments.get("max_minutes", 8))
        max_steps = int(arguments.get("max_steps", 15))
        emit_thread = bool(arguments.get("emit_thread", False))
        watcher_interval = float(arguments.get("watcher_interval_seconds", 4.0))

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "ANTHROPIC_API_KEY required for swarm_run"}, indent=2))]

        # Pull Thread context if domain hinted
        thread_ctx: dict = {}
        if thread_domain:
            try:
                pb_live = load_playbook()
                d_entry = pb_live.get(thread_domain) or pb_live.get(thread_domain.replace("www.", "")) or {}
                thread_ctx = {
                    "domain": thread_domain,
                    "notes": d_entry.get("notes", [])[:10],
                    "proven_actions": d_entry.get("proven_actions", [])[:15],
                    "preflight": d_entry.get("preflight", [])[:10],
                    "anti_bot_signals": d_entry.get("anti_bot_signals", []),
                    "framework": d_entry.get("framework"),
                }
            except Exception:
                pass

        # Shared state between agents
        state = {
            "events": [],            # ordered log
            "watcher_alert": None,   # current alert (cleared after driver handles)
            "should_stop": False,
            "driver_actions": [],
            "watcher_alerts": [],
            "step": 0,
        }

        async def _call_claude(system: str, messages: list, max_tokens: int = 800, with_image: str | None = None) -> dict:
            import urllib.request as _req
            content_list: list = []
            if with_image:
                content_list.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": with_image}})
            # Inject latest user message as final content
            messages_with_image = list(messages)
            if content_list and messages_with_image:
                last_user = messages_with_image[-1]
                if last_user.get("role") == "user":
                    if isinstance(last_user["content"], str):
                        last_user = {"role": "user", "content": content_list + [{"type": "text", "text": last_user["content"]}]}
                    messages_with_image[-1] = last_user
            body = {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages_with_image,
            }
            req = _req.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(body).encode(),
                headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            )
            loop = asyncio.get_event_loop()
            def _do():
                return _req.urlopen(req, timeout=25).read()
            raw = await loop.run_in_executor(None, _do)
            return json.loads(raw.decode())

        async def watcher_loop():
            """Snap a screenshot every watcher_interval seconds. Ask Haiku: any
            popups, captchas, error modals, session expiry, blocking overlays?
            If yes, publish an alert that the driver checks before each action."""
            watcher_system = (
                "You are the WATCHER agent in a 2-agent browser automation swarm. Your ONLY job is to look at the "
                "current screenshot and answer: is there anything UNUSUAL blocking the main flow? Things to flag: "
                "popups, modals, captchas, 'session expired' notices, error toasts, cookie consent overlays, "
                "rate-limit warnings, unexpected confirmation dialogs. Do NOT flag normal UI like nav menus, "
                "sidebars, or expected content. "
                "Respond ONLY with JSON: {\"alert\": true|false, \"what\": \"short description\", \"action_hint\": "
                "\"what the driver should do (dismiss, accept_cookies, solve_captcha, pause_for_human, ignore)\"}."
            )
            while not state["should_stop"]:
                try:
                    shot = await cdp_send(ws_url, "Page.captureScreenshot", {"format": "png"})
                    png_b64 = shot.get("data") or shot.get("result", {}).get("data") or ""
                    if png_b64:
                        resp = await _call_claude(
                            watcher_system,
                            [{"role": "user", "content": "Check the current page state."}],
                            max_tokens=200,
                            with_image=png_b64,
                        )
                        text = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text").strip()
                        # Extract JSON
                        try:
                            jstart = text.find("{"); jend = text.rfind("}")
                            if jstart >= 0 and jend > jstart:
                                obj = json.loads(text[jstart:jend + 1])
                                if obj.get("alert"):
                                    alert_entry = {"ts": _t.time(), **obj}
                                    state["watcher_alert"] = alert_entry
                                    state["watcher_alerts"].append(alert_entry)
                                    state["events"].append({"role": "watcher", "kind": "alert", **obj})
                        except Exception:
                            pass
                except Exception:
                    pass
                # Wait for next interval, but bail fast if swarm stopped
                slept = 0
                while slept < watcher_interval and not state["should_stop"]:
                    await asyncio.sleep(0.2)
                    slept += 0.2

        async def driver_loop():
            """Decision loop: ask Haiku what action to take next given the goal,
            Thread context, last actions, and any pending Watcher alert."""
            driver_system = (
                "You are the DRIVER agent in a 2-agent browser automation swarm. You are working toward a USER GOAL "
                "on a website. You have access to the WebLoom engine's tools. You have an installed Thread with "
                "proven_actions you can lean on. A separate WATCHER agent monitors the page for popups/captchas/etc "
                "— if it raises an alert, address that before proceeding to the goal.\n\n"
                "Respond ONLY with JSON of shape: {\"thought\": \"...\", \"tool\": \"...\", \"args\": {...}, "
                "\"done\": false}. To finish, set done=true with no tool. To halt with an error, set "
                "tool=\"pause_for_human\" with args.reason.\n\n"
                "Available tools (subset): navigate, scan_tab, click, fill, key_press, key_type, "
                "lexical_set_text, draftjs_set_text, scroll_tab, wait_for, screenshot, vision_check, "
                "click_at_coords, upload_file, replay_xhr, pause_for_human. "
                "Always include session and tab in args when relevant."
            )

            for step_i in range(max_steps):
                if state["should_stop"]:
                    break
                state["step"] = step_i

                # If Watcher has a pending alert, handle that first
                alert = state["watcher_alert"]
                state["watcher_alert"] = None

                hist_summary = state["driver_actions"][-5:]
                user_msg = json.dumps({
                    "goal": goal,
                    "thread_context": thread_ctx,
                    "watcher_alert": alert,
                    "session": arguments.get("session", "default"),
                    "tab": arguments.get("tab", ""),
                    "recent_actions": hist_summary,
                    "step": step_i,
                    "max_steps": max_steps,
                })

                try:
                    resp = await _call_claude(driver_system, [{"role": "user", "content": user_msg}], max_tokens=500)
                except Exception as e:
                    state["events"].append({"role": "driver", "kind": "error", "error": str(e)})
                    break
                text = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text").strip()
                try:
                    jstart = text.find("{"); jend = text.rfind("}")
                    obj = json.loads(text[jstart:jend + 1]) if jstart >= 0 and jend > jstart else None
                except Exception:
                    obj = None
                if not obj:
                    state["events"].append({"role": "driver", "kind": "parse_fail", "raw": text[:300]})
                    break

                if obj.get("done"):
                    state["events"].append({"role": "driver", "kind": "done", "thought": obj.get("thought", "")})
                    break

                tool_to_run = obj.get("tool")
                tool_args = obj.get("args", {}) or {}
                tool_args.setdefault("session", arguments.get("session", "default"))
                if arguments.get("tab"):
                    tool_args.setdefault("tab", arguments["tab"])

                action_entry = {"step": step_i, "thought": obj.get("thought", "")[:200], "tool": tool_to_run, "args": tool_args}
                state["events"].append({"role": "driver", "kind": "action", **action_entry})

                try:
                    res = await _execute_tool_action(tool_to_run, tool_args)
                    out_text = ""
                    if isinstance(res, list):
                        for c in res:
                            if hasattr(c, "text"):
                                out_text = c.text
                                break
                    action_entry["output"] = out_text[:500]
                    state["driver_actions"].append(action_entry)
                except Exception as e:
                    action_entry["error"] = str(e)
                    state["driver_actions"].append(action_entry)
                    state["events"].append({"role": "driver", "kind": "tool_error", "error": str(e)})
                    break

                # Brief pacing between actions so Watcher can catch popups
                await asyncio.sleep(0.5)

        # Launch swarm with overall timeout
        start_ts = _t.time()
        async def with_timeout():
            try:
                await asyncio.wait_for(
                    asyncio.gather(watcher_loop(), driver_loop(), return_exceptions=True),
                    timeout=max_minutes * 60,
                )
            except asyncio.TimeoutError:
                state["events"].append({"role": "swarm", "kind": "timeout"})

        # When driver finishes (done or error), tell watcher to stop too
        async def finalize():
            await driver_loop()
            state["should_stop"] = True

        async def run_pair():
            try:
                await asyncio.wait_for(
                    asyncio.gather(watcher_loop(), finalize(), return_exceptions=True),
                    timeout=max_minutes * 60,
                )
            except asyncio.TimeoutError:
                state["events"].append({"role": "swarm", "kind": "timeout"})
                state["should_stop"] = True

        await run_pair()
        duration = int(_t.time() - start_ts)

        # Optional: emit a Thread proposal from the successful actions
        thread_proposal = None
        if emit_thread:
            proven = []
            for a in state["driver_actions"]:
                if a.get("error"): continue
                tool_name = a.get("tool", "")
                if tool_name in ("scan_tab", "screenshot", "read_tab"): continue  # skip read-only
                proven.append({
                    "descriptor": a.get("thought", "")[:80] or f"{tool_name} action",
                    "strategy": tool_name,
                    "args_sketch": {k: v for k, v in a.get("args", {}).items() if k not in ("session", "tab")},
                    "successes": 1,
                })
            if thread_domain:
                thread_proposal = {
                    "domain": thread_domain,
                    "name": f"{thread_domain} — swarm-authored",
                    "version": "0.1.0",
                    "tier": "mapped",
                    "author": "swarm",
                    "notes": [f"Swarm-authored from goal: {goal[:200]}"],
                    "proven_actions": proven,
                    "created_at": int(_t.time()),
                    "created_by": "swarm_run",
                }

        status = (
            "completed" if any(e.get("kind") == "done" for e in state["events"])
            else "timeout" if any(e.get("kind") == "timeout" for e in state["events"])
            else "max_steps" if state["step"] >= max_steps - 1
            else "aborted"
        )

        return [TextContent(type="text", text=json.dumps({
            "ok": status == "completed",
            "goal": goal,
            "status": status,
            "duration_seconds": duration,
            "steps_executed": len(state["driver_actions"]),
            "watcher_alerts_seen": len(state["watcher_alerts"]),
            "driver_actions": state["driver_actions"],
            "watcher_alerts": state["watcher_alerts"],
            "events": state["events"][-50:],  # tail
            "thread_proposal": thread_proposal,
        }, indent=2))]

    # ── VISUAL DIFF PREFLIGHT ────────────────────────────────────────────
    if name == "visual_diff_preflight":
        import base64
        selector = arguments["selector"]
        anchor_name = arguments["anchor_name"]
        threshold = float(arguments.get("threshold", 0.85))
        mode = arguments.get("mode", "auto")

        # Get element bounding box
        coord_js = f"""(function(s){{
            const el = document.querySelector(s);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) return null;
            return {{x: r.left, y: r.top, w: r.width, h: r.height}};
        }})({json.dumps(selector)})"""
        coord_r = await eval_in_tab(ws_url, coord_js)
        bbox = coord_r.get("result", {}).get("value")
        if not bbox:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"element not found or too small: {selector}"}, indent=2))]

        # Snap clipped screenshot
        clip = {"x": bbox["x"], "y": bbox["y"], "width": bbox["w"], "height": bbox["h"], "scale": 1}
        try:
            shot = await cdp_send(ws_url, "Page.captureScreenshot", {"format": "png", "clip": clip})
            png_b64 = shot.get("data") or shot.get("result", {}).get("data") or ""
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"screenshot failed: {e}"}, indent=2))]
        if not png_b64:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "empty screenshot"}, indent=2))]

        # Compute a lightweight perceptual fingerprint without external deps:
        # downsample to 16x16 grayscale by reading PNG bytes via stdlib + hashing.
        # We don't need true perceptual hashing — pixel-grid hash is enough to detect
        # significant visual change (color shift, content swap, button restyle).
        import hashlib, zlib, struct
        try:
            raw = base64.b64decode(png_b64)
            # Crude fingerprint: SHA256 of the PNG bytes themselves.
            # For real perceptual hash we'd need PIL; this is "exact-or-changed".
            # We compensate with a SECOND fingerprint: the size + first/last 64 bytes
            # of the IDAT chunk, which approximates pixel content.
            sha = hashlib.sha256(raw).hexdigest()
            # Find IDAT chunk for a coarser content fingerprint
            idat_sample = ""
            i = 8  # skip PNG header
            while i < len(raw) - 8:
                length = struct.unpack(">I", raw[i:i+4])[0]
                ctype = raw[i+4:i+8]
                if ctype == b"IDAT" and length > 0:
                    sample = raw[i+8:i+8+min(length, 256)]
                    idat_sample = hashlib.sha256(sample).hexdigest()[:32]
                    break
                i += 8 + length + 4
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"hash failed: {e}"}, indent=2))]

        # Load + store visual anchor in the playbook
        cur = await eval_in_tab(ws_url, "location.href")
        dom = domain_from_url(cur.get("result", {}).get("value", ""))
        if not dom:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "no domain"}, indent=2))]

        pb = _load_live_playbook_raw()
        if dom not in pb:
            pb[dom] = {}
        anchors = pb[dom].setdefault("visual_anchors", {})
        existing = anchors.get(anchor_name)

        if mode == "record" or (mode == "auto" and not existing):
            anchors[anchor_name] = {"png_sha": sha, "idat_sample": idat_sample, "selector": selector, "recorded_at": int(__import__("time").time())}
            save_playbook_data(pb)
            return [TextContent(type="text", text=json.dumps({"ok": True, "mode": "recorded", "anchor_name": anchor_name, "png_sha": sha[:16]}, indent=2))]

        # Compare mode
        if not existing:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"no anchor '{anchor_name}' recorded for {dom}; call with mode='record' first"}, indent=2))]
        # Exact PNG match = 1.0; matching IDAT sample but different overall = 0.6;
        # different IDAT entirely = 0.0. Crude but works as a drift signal.
        if sha == existing.get("png_sha"):
            similarity = 1.0
        elif idat_sample and idat_sample == existing.get("idat_sample"):
            similarity = 0.7
        else:
            similarity = 0.0
        drift = similarity < threshold
        return [TextContent(type="text", text=json.dumps({
            "ok": True, "mode": "compared", "anchor_name": anchor_name,
            "similarity": similarity, "threshold": threshold, "drift_detected": drift,
            "hint": "If drift_detected, run drift_heal_suggest or pause_for_human." if drift else None,
        }, indent=2))]

    # ── WEAVE — semantic compose layer ──────────────────────────────────
    if name == "weave":
        goal = arguments["goal"]
        ctx = arguments.get("context") or {}
        dry_run = bool(arguments.get("dry_run"))
        sess_for_exec = arguments.get("session", "default")

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        # Build the catalog of installed Threads for the planner
        thread_summary = []
        for t in load_playbook().values() if False else []:
            pass
        # Better: iterate the live playbook + installed thread files
        live = _load_live_playbook_raw()
        for d, info in list(live.items())[:80]:
            actions = info.get("action_log", {})
            if actions:
                top = sorted(actions.items(), key=lambda kv: (kv[1].get("successes", 0)), reverse=True)[:5]
                thread_summary.append({
                    "domain": d,
                    "top_actions": [{"desc": k, "kind": v.get("kind", "?"), "successes": v.get("successes", 0)} for k, v in top],
                })

        if not anthropic_key:
            return [TextContent(type="text", text=json.dumps({
                "ok": False,
                "error": "ANTHROPIC_API_KEY not set",
                "hint": "weave() needs Claude for planning. Set ANTHROPIC_API_KEY or compose tool calls manually.",
                "available_threads": [t["domain"] for t in thread_summary],
            }, indent=2))]

        planner_system = (
            "You are the WebLoom action planner. Given a user goal and a catalog of installed Threads (per-site "
            "recipes), produce a JSON action plan that, when executed against the WebLoom engine's tools, achieves "
            "the goal. Use one of these tools per step: navigate, click, fill, lexical_set_text, draftjs_set_text, "
            "key_press, key_type, scroll_tab, wait_for, screenshot, vision_check, click_at_coords, upload_file, "
            "replay_xhr, pause_for_human. "
            "Respond ONLY with JSON of shape: {\"plan\": [{\"tool\": \"...\", \"args\": {...}, \"why\": \"...\"}], "
            "\"summary\": \"...\"}. Keep the plan short (5-10 steps max). When unsure, insert a pause_for_human step. "
            "Never invent selectors not present in the Thread catalog."
        )
        planner_user = json.dumps({
            "goal": goal,
            "context": ctx,
            "installed_threads": thread_summary,
        })

        import urllib.request as _req
        try:
            body = {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1200,
                "system": planner_system,
                "messages": [{"role": "user", "content": planner_user}],
            }
            req = _req.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(body).encode(),
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            with _req.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"planner call failed: {e}"}, indent=2))]

        plan_text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text").strip()
        # Extract first JSON object from the response
        plan_obj = None
        try:
            jstart = plan_text.find("{")
            jend = plan_text.rfind("}")
            if jstart >= 0 and jend > jstart:
                plan_obj = json.loads(plan_text[jstart:jend + 1])
        except Exception:
            pass
        if not plan_obj or "plan" not in plan_obj:
            return [TextContent(type="text", text=json.dumps({
                "ok": False, "error": "planner returned unparseable response",
                "raw": plan_text[:500],
            }, indent=2))]

        plan = plan_obj.get("plan", [])
        summary = plan_obj.get("summary", "")

        if dry_run:
            return [TextContent(type="text", text=json.dumps({
                "ok": True, "dry_run": True, "summary": summary, "plan": plan,
                "next": "Call again with dry_run=false to execute, OR run each step yourself.",
            }, indent=2))]

        # Execute the plan via the same dispatcher used by replay_recipe
        results = []
        for i, step in enumerate(plan):
            tool_name = step.get("tool")
            tool_args = step.get("args", {}) or {}
            # Inject session if missing
            if "session" in (arguments.get("context") or {}):
                tool_args.setdefault("session", arguments["context"]["session"])
            tool_args.setdefault("session", sess_for_exec)
            try:
                res = await _execute_tool_action(tool_name, tool_args)
                text_out = ""
                if isinstance(res, list):
                    for c in res:
                        if hasattr(c, "text"):
                            text_out = c.text
                            break
                results.append({"step": i, "tool": tool_name, "ok": True, "output": text_out[:600]})
            except Exception as e:
                results.append({"step": i, "tool": tool_name, "ok": False, "error": str(e)})
                break  # Abort plan on first failure — safer default
        return [TextContent(type="text", text=json.dumps({
            "ok": all(r.get("ok") for r in results),
            "summary": summary,
            "executed": len(results),
            "total_planned": len(plan),
            "results": results,
        }, indent=2))]

    # ── WEBSOCKET SUBSCRIPTION ───────────────────────────────────────────
    if name == "subscribe_to_websocket":
        pattern = arguments["pattern"]
        buffer_id = arguments["buffer_id"]
        max_buffer = int(arguments.get("max_buffer", 100))
        is_regex = pattern.startswith("regex:")
        regex = None
        if is_regex:
            try:
                regex = re.compile(pattern[6:])
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"invalid regex: {e}"}, indent=2))]
        substr = pattern if not is_regex else None

        conn = await _get_conn(ws_url)
        await conn.send("Network.enable")

        # Init the shared buffer store
        if not hasattr(_get_conn, "_ws_buffers"):
            _get_conn._ws_buffers = {}  # type: ignore
        buffers = _get_conn._ws_buffers  # type: ignore
        key = (ws_url, buffer_id)
        buffers[key] = {"messages": [], "max": max_buffer, "pattern": pattern}

        def on_frame(msg):
            p = msg.get("params", {})
            response = p.get("response", {})
            payload = response.get("payloadData", "") or ""
            opcode = response.get("opcode")
            matched = False
            if is_regex and regex.search(payload):
                matched = True
            elif substr and substr in payload:
                matched = True
            if not matched:
                return
            buf = buffers.get(key)
            if not buf:
                return
            buf["messages"].append({"ts": p.get("timestamp"), "opcode": opcode, "payload": payload[:4000]})
            if len(buf["messages"]) > buf["max"]:
                buf["messages"] = buf["messages"][-buf["max"]:]

        conn.subscribe("Network.webSocketFrameReceived", on_frame)
        buffers[key]["unsub"] = on_frame  # store handle for later removal
        return [TextContent(type="text", text=json.dumps({"ok": True, "buffer_id": buffer_id, "pattern": pattern}, indent=2))]

    if name == "poll_websocket_messages":
        buffer_id = arguments["buffer_id"]
        max_n = int(arguments.get("max", 50))
        buffers = getattr(_get_conn, "_ws_buffers", {})
        key = (ws_url, buffer_id)
        buf = buffers.get(key)
        if not buf:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"no buffer '{buffer_id}' — call subscribe_to_websocket first"}, indent=2))]
        msgs = buf["messages"][:max_n]
        buf["messages"] = buf["messages"][max_n:]
        return [TextContent(type="text", text=json.dumps({
            "ok": True, "buffer_id": buffer_id, "messages": msgs,
            "remaining_in_buffer": len(buf["messages"]),
        }, indent=2))]

    # ── EPISODIC MEMORY ──────────────────────────────────────────────────
    if name == "episodic_remember":
        summary = arguments["summary"]
        state = arguments.get("state") or {}
        import time as _t
        cur = await eval_in_tab(ws_url, "location.href")
        dom = domain_from_url(cur.get("result", {}).get("value", "")) or "unknown"
        epi_dir = Path.home() / ".webloom" / "episodic"
        epi_dir.mkdir(parents=True, exist_ok=True)
        file = epi_dir / f"{dom}.json"
        existing = []
        if file.exists():
            try:
                existing = json.loads(file.read_text())
            except Exception:
                existing = []
        entry = {
            "ts": int(_t.time()),
            "domain": dom,
            "summary": summary[:1000],
            "state": state,
            "url": cur.get("result", {}).get("value", ""),
        }
        existing.append(entry)
        existing = existing[-50:]  # cap at 50 per domain
        file.write_text(json.dumps(existing, indent=2))
        return [TextContent(type="text", text=json.dumps({"ok": True, "domain": dom, "stored_at": file.name, "total_episodes": len(existing)}, indent=2))]

    if name == "episodic_recall":
        limit = int(arguments.get("limit", 5))
        cur = await eval_in_tab(ws_url, "location.href")
        dom = arguments.get("domain") or domain_from_url(cur.get("result", {}).get("value", "")) or "unknown"
        file = Path.home() / ".webloom" / "episodic" / f"{dom}.json"
        if not file.exists():
            return [TextContent(type="text", text=json.dumps({"ok": True, "domain": dom, "episodes": [], "hint": "No episodic memory stored for this domain yet."}, indent=2))]
        try:
            existing = json.loads(file.read_text())
        except Exception:
            existing = []
        recent = existing[-limit:][::-1]  # most recent first
        latest_state = recent[0].get("state") if recent else {}
        return [TextContent(type="text", text=json.dumps({
            "ok": True, "domain": dom, "total_stored": len(existing),
            "episodes": recent, "latest_state": latest_state,
        }, indent=2))]

    # ── #9 DRIFT HEAL SUGGEST ───────────────────────────────────────────
    if name == "drift_heal_suggest":
        old_sel = arguments["old_selector"]
        desc = arguments["descriptor"]
        propose = bool(arguments.get("propose_to_marketplace", False))
        # Scan the current DOM for elements that look like they could replace old_sel.
        # Heuristics: matching role, accessibility name (textContent/aria-label), text similarity to descriptor,
        # surviving structural anchor (data-testid, id, name), and visibility.
        js = """(function(oldSel, desc) {
            const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\\s+/g, ' ').trim();
            const descTerms = norm(desc).split(' ').filter(t => t.length > 2);
            const all = Array.from(document.querySelectorAll('button, a, [role="button"], input, [contenteditable], [data-testid], [id], [name]'));
            const scored = [];
            for (const el of all) {
                if (!(el.offsetWidth || el.offsetHeight)) continue;  // not visible
                const text = norm((el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || el.value || '').slice(0, 200));
                const role = el.getAttribute('role') || el.tagName.toLowerCase();
                const testid = el.getAttribute('data-testid');
                const id = el.id;
                const name = el.getAttribute('name');
                // Score: text overlap with descriptor terms + structural anchor bonus
                let score = 0;
                for (const t of descTerms) if (text.includes(t)) score += 2;
                if (testid) score += 3;
                if (id) score += 2;
                if (name) score += 1;
                if (role === 'button' && /button|submit|click|post/.test(norm(desc))) score += 1;
                if (score < 2) continue;
                // Build a stable selector
                let stableSel = null;
                if (testid) stableSel = '[data-testid="' + testid.replace(/"/g, '\\\\"') + '"]';
                else if (id) stableSel = '#' + CSS.escape(id);
                else if (name) stableSel = el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\\\"') + '"]';
                else stableSel = el.tagName.toLowerCase() + ':contains("' + text.slice(0, 30) + '")';  // non-standard, hint only
                scored.push({
                    selector: stableSel,
                    score: score,
                    text: text.slice(0, 80),
                    tag: el.tagName.toLowerCase(),
                    role: role,
                    reason: testid ? 'data-testid present' : id ? 'id present' : name ? 'name attr' : 'text match',
                });
            }
            scored.sort((a, b) => b.score - a.score);
            return JSON.stringify({ ok: true, candidates: scored.slice(0, 8) });
        })(""" + json.dumps(old_sel) + ", " + json.dumps(desc) + ")"
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        # If caller opted in to propose to marketplace, send the top candidate
        # to the patch-proposal endpoint. ALWAYS author-gated: this adds a
        # proposal row to the queue; nothing auto-applies. Author reviews via
        # /admin/marketplace and clicks approve to ship the patch to all buyers.
        if propose:
            try:
                parsed_drift = json.loads(val) if isinstance(val, str) else {}
                cands = parsed_drift.get("candidates", []) if isinstance(parsed_drift, dict) else []
                if cands:
                    top = cands[0]
                    cur_url = await eval_in_tab(ws_url, "location.href")
                    dom = domain_from_url(cur_url.get("result", {}).get("value", ""))
                    if dom and top.get("selector"):
                        _propose_patch_fire_forget({
                            "anon_id": _get_anon_id(),
                            "domain": dom,
                            "kind": "selector_replace",
                            "old_selector": old_sel,
                            "new_selector": top.get("selector"),
                            "descriptor": desc,
                            "confidence": min(1.0, (int(top.get("score", 0)) / 10.0)),
                            "engine_version": ENGINE_VERSION,
                        })
            except Exception:
                pass
        return [TextContent(type="text", text=f"drift_heal_suggest: {val}")]

    # ── #7 PARALLEL ORCHESTRATOR ────────────────────────────────────────
    if name == "run_parallel":
        calls = arguments.get("calls", [])
        max_conc = int(arguments.get("max_concurrency", 4))
        sem = asyncio.Semaphore(max(1, max_conc))

        async def _one(idx, item):
            tool_name = item.get("tool")
            tool_args = item.get("args", {}) or {}
            async with sem:
                try:
                    # Recurse into call_tool by directly invoking the dispatcher.
                    # We reuse this function's enclosing handlers by calling _execute_tool_action,
                    # which is the same dispatcher recording.replay_recipe uses.
                    res = await _execute_tool_action(tool_name, tool_args)
                    text_out = ""
                    if isinstance(res, list):
                        for c in res:
                            if hasattr(c, "text"):
                                text_out = c.text
                                break
                    return {"idx": idx, "tool": tool_name, "ok": True, "result": text_out[:2000]}
                except Exception as e:
                    return {"idx": idx, "tool": tool_name, "ok": False, "error": str(e)}

        tasks = [asyncio.create_task(_one(i, item)) for i, item in enumerate(calls)]
        results = await asyncio.gather(*tasks)
        return [TextContent(type="text", text=json.dumps({
            "ok": True,
            "count": len(results),
            "results": results,
        }, indent=2))]

    if name == "reddit_submit_comment":
        post_url = arguments["post_url"]
        markdown = arguments["markdown"]
        verify = bool(arguments.get("verify_landed", True))
        trace: list[str] = []

        # 1. Navigate to the post if not already there
        cur_url_r = await eval_in_tab(ws_url, "location.href")
        cur_url = cur_url_r.get("result", {}).get("value", "")
        if not cur_url.startswith(post_url.split("?")[0]):
            await cdp_send(ws_url, "Page.navigate", {"url": post_url})
            # poll for ready
            for _ in range(50):
                r = await eval_in_tab(ws_url, "document.readyState")
                if r.get("result", {}).get("value") == "complete":
                    break
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.5)
            trace.append(f"navigated to {post_url}")
        else:
            trace.append("already on target post")

        # 2. Click the comment composer placeholder to mount Lexical
        #    Reddit's selectors have churned; we try several known ones in order.
        placeholder_selectors = [
            'shreddit-async-loader[bundlename="comment_composer"] [contenteditable]',
            'faceplate-tracker[noun="comment_composer"] [contenteditable]',
            'div[role="textbox"][aria-label*="comment" i]',
            'div[contenteditable=true][aria-label*="comment" i]',
            'div[contenteditable=true][data-lexical-editor=true]',
            '[data-testid="comment-submission-form"] [contenteditable]',
            'shreddit-composer textarea, shreddit-composer [contenteditable]',
        ]
        click_targets = [
            'shreddit-async-loader[bundlename="comment_composer"]',
            '[data-testid="comment-submission-form-richtext"]',
            'div[role="textbox"]',
            'faceplate-tracker[noun="comment_composer"]',
        ]
        # First: try to click a placeholder/wrapper that mounts the composer
        clicked = False
        for sel in click_targets:
            r = await eval_in_tab(ws_url, f"!!document.querySelector({json.dumps(sel)})")
            if r.get("result", {}).get("value"):
                await eval_in_tab(ws_url, f"(function(){{const el=document.querySelector({json.dumps(sel)}); el && el.scrollIntoView({{block:'center'}}); el && el.click(); return true;}})()")
                clicked = True
                trace.append(f"clicked composer trigger: {sel}")
                break
        if not clicked:
            trace.append("no composer trigger matched — assuming composer is already mounted")

        await asyncio.sleep(0.6)

        # 3. Find a contenteditable now visible
        container_sel = None
        for sel in placeholder_selectors:
            r = await eval_in_tab(ws_url, f"!!document.querySelector({json.dumps(sel)})")
            if r.get("result", {}).get("value"):
                container_sel = sel
                break
        if not container_sel:
            return [TextContent(type="text", text=(
                "reddit_submit_comment: could not locate comment composer contenteditable. "
                "Reddit's selectors may have churned; check current DOM and pass container_selector directly to lexical_set_text.\n\n"
                "Trace:\n" + "\n".join(trace)
            ))]
        trace.append(f"composer found: {container_sel}")

        # 4. Set text via lexical_set_text equivalent (inline the JS)
        set_arguments = {
            "container_selector": container_sel,
            "text": markdown,
            "mount_timeout_seconds": 4,
        }
        # Re-route into our lexical_set_text handler by recursion
        sub = await call_tool("lexical_set_text", {**arguments, **set_arguments})
        sub_text = sub[0].text if sub else ""
        trace.append(f"lexical_set_text: {sub_text[:200]}")

        # 5. Find + click submit button — SCOPED to the composer ancestor of the
        #    focused contenteditable. Reddit pages have ~22 button[type=submit]
        #    (upvote, sort, video, "Open user actions", etc.) — the bare selector
        #    matches the wrong one. Walk up from the editable to find the composer,
        #    then search inside it; fall back to matching by visible button text.
        await asyncio.sleep(0.4)
        submit_js = """(function() {
            const findSubmit = () => {
                // Find currently-focused contenteditable (just-typed-into)
                let editable = document.activeElement;
                if (editable && !editable.isContentEditable) {
                    editable = editable.querySelector?.('[contenteditable=true]') || editable;
                }
                if (!editable || !editable.isContentEditable) {
                    // Fall back to any visible contenteditable
                    const all = document.querySelectorAll('[contenteditable=true]');
                    for (const e of all) {
                        if (e.offsetParent !== null) { editable = e; break; }
                    }
                }
                if (!editable) return {error: 'no contenteditable to anchor from'};

                // Walk up to find the composer container
                const composerSel =
                    'form, shreddit-composer, shreddit-async-loader, [class*="composer" i], '
                    + '[class*="comment-form" i], [data-testid*="comment" i], [data-testid*="composer" i], '
                    + '[role="dialog"]';
                let composer = editable;
                while (composer && composer !== document.body) {
                    if (composer.matches && composer.matches(composerSel)) break;
                    composer = composer.parentElement;
                }
                if (!composer || composer === document.body) {
                    composer = document.querySelector(
                        'shreddit-async-loader[bundlename="comment_composer"], '
                        + '[data-testid="comment-submission-form"], '
                        + 'shreddit-composer'
                    );
                }
                if (!composer) return {error: 'no composer container found around editable'};

                const found_in = composer.tagName + (composer.id ? '#'+composer.id : '');

                // Inside the composer, find the submit button.
                // Priority order: explicit text match → type=submit → role=button with submit-y text
                const textKeys = ['comment', 'comentar', 'post', 'publicar', 'submit', 'reply', 'responder', 'send', 'enviar'];
                const allBtns = composer.querySelectorAll('button:not([disabled]), [role=button]:not([aria-disabled=true])');
                for (const b of allBtns) {
                    const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
                    if (textKeys.some(k => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k))) {
                        return {btn: b, found_in, matched_text: t.slice(0, 40)};
                    }
                }
                // Fallback: type=submit inside the composer
                const ts = composer.querySelector('button[type="submit"]:not([disabled])');
                if (ts) return {btn: ts, found_in, matched_text: (ts.textContent || '').trim().slice(0, 40)};
                return {error: 'composer found but no submit button inside it', found_in};
            };
            const r = findSubmit();
            if (r.error) return JSON.stringify(r);
            const b = r.btn;
            b.scrollIntoView({block:'center'});
            const fire = t => new MouseEvent(t, {bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window});
            b.dispatchEvent(fire('mousedown'));
            b.dispatchEvent(fire('mouseup'));
            b.click();
            return JSON.stringify({ok: true, found_in: r.found_in, matched_text: r.matched_text});
        })()"""
        r = await eval_in_tab(ws_url, submit_js)
        sub_val = r.get("result", {}).get("value", "{}")
        trace.append(f"submit: {sub_val}")
        try:
            sub_data = json.loads(sub_val)
            if not sub_data.get("ok"):
                return [TextContent(type="text", text=(
                    f"reddit_submit_comment: submit-button discovery failed.\n\n{sub_data.get('error','?')}\n\nTrace:\n" + "\n".join(trace)
                ))]
        except Exception:
            pass

        # 6. Wait for the post to register + optionally verify
        await asyncio.sleep(2.5)
        landed = True  # assume success if we reached submit without errors
        if verify:
            check_js = f"""(function() {{
                const target = {json.dumps(markdown[:60])};
                const matches = Array.from(document.querySelectorAll('p, div')).filter(el =>
                    el.textContent && el.textContent.includes(target)
                );
                return JSON.stringify({{found: matches.length, sample: matches[0] ? matches[0].textContent.slice(0, 120) : null}});
            }})()"""
            r = await eval_in_tab(ws_url, check_js)
            ver = r.get("result", {}).get("value", "{}")
            trace.append(f"verify: {ver}")
            try:
                ver_data = json.loads(ver)
                landed = int(ver_data.get("found", 0)) > 0
            except Exception:
                landed = False

        # Record into playbook — submitting a Reddit comment is a textbook
        # proven_action for the reddit.com Thread.
        try:
            d = domain_from_url(post_url) or "reddit.com"
            _playbook_record(d, "reddit_submit_comment", "composite", landed, kind="submit_comment", selector_pattern=post_url)
        except Exception:
            pass

        return [TextContent(type="text", text="reddit_submit_comment complete.\n\nTrace:\n" + "\n".join(trace))]

    if name == "touch_tap":
        sel = arguments["selector"]
        double = bool(arguments.get("double", False))
        # Get center coords of the target
        coord_js = """(function(s) {
            const el = document.querySelector(s);
            if (!el) return null;
            el.scrollIntoView({block:'center', inline:'center', behavior:'instant'});
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return null;
            return JSON.stringify({x: r.left + r.width/2, y: r.top + r.height/2});
        })(""" + json.dumps(sel) + ")"
        r = await eval_in_tab(ws_url, coord_js)
        val = r.get("result", {}).get("value")
        if not val:
            return [TextContent(type="text", text=f"touch_tap: element not found or zero-size: {sel}")]
        try:
            coords = json.loads(val)
        except Exception:
            return [TextContent(type="text", text=f"touch_tap: coord parse failed: {val}")]
        x, y = coords["x"], coords["y"]

        # Enable touch emulation (idempotent — Chrome ignores if already on)
        try:
            await cdp_send(ws_url, "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
        except Exception:
            pass

        async def _tap():
            await cdp_send(ws_url, "Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [{"x": x, "y": y, "id": 1, "radiusX": 5, "radiusY": 5, "force": 1.0}],
            })
            await asyncio.sleep(0.05)
            await cdp_send(ws_url, "Input.dispatchTouchEvent", {
                "type": "touchEnd",
                "touchPoints": [],
            })

        await _tap()
        if double:
            await asyncio.sleep(0.1)
            await _tap()
        try:
            tab_url_r = await eval_in_tab(ws_url, "location.href")
            tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
            if tab_dom:
                _playbook_record(tab_dom, f"touch_tap:{sel}", "touch_tap" + ("_double" if double else ""), True,
                                 kind="touch", selector_pattern=sel)
        except Exception:
            pass
        return [TextContent(type="text", text=f"touch_tap{' (double)' if double else ''} @ ({x:.0f},{y:.0f}) on {sel}")]

    if name == "replay_xhr":
        url = arguments["url"]
        method = arguments.get("method", "POST")
        headers = arguments.get("headers", {}) or {}
        body = arguments.get("body", None)
        params = arguments.get("params", {}) or {}

        # Merge query params into URL
        if params:
            try:
                from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
                u = urlparse(url)
                existing = dict(parse_qsl(u.query))
                existing.update(params)
                url = urlunparse(u._replace(query=urlencode(existing)))
            except Exception:
                pass

        # Body normalization
        body_payload = "null"
        is_json = False
        if body is None:
            body_payload = "null"
        elif isinstance(body, (dict, list)):
            body_payload = json.dumps(body)
            is_json = True
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body_payload = json.dumps(body)  # quoted string literal in JS
        else:
            body_payload = json.dumps(str(body))

        js = f"""(async function(url, method, headers, body, isJson) {{
            try {{
                const opts = {{ method, credentials: 'include', headers }};
                if (body !== null && body !== undefined && method !== 'GET' && method !== 'HEAD') {{
                    opts.body = isJson ? body : body;  // body is already serialized server-side
                }}
                const res = await fetch(url, opts);
                const ct = res.headers.get('content-type') || '';
                let bodyOut;
                try {{
                    if (ct.includes('json')) bodyOut = JSON.stringify(await res.json());
                    else bodyOut = await res.text();
                }} catch(e) {{ bodyOut = '<could not read body: ' + e.message + '>'; }}
                return JSON.stringify({{
                    ok: res.ok, status: res.status, statusText: res.statusText,
                    final_url: res.url, content_type: ct,
                    body: (bodyOut || '').slice(0, 3000)
                }});
            }} catch (e) {{
                return JSON.stringify({{ok:false, error: 'fetch threw: ' + (e && e.message)}});
            }}
        }})({json.dumps(url)}, {json.dumps(method)}, {json.dumps(headers)}, {body_payload}, {json.dumps(is_json)})"""
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")

        # Auto-record XHR-replay successes into the playbook. Without this,
        # major discoveries (e.g. the X CreateTweet GraphQL replay) stay
        # invisible to the engine. Descriptor uses a normalized path so
        # rotating hash segments collapse to one entry.
        try:
            parsed = json.loads(val) if isinstance(val, str) else (val or {})
            ok = bool(parsed.get("ok"))
            status = parsed.get("status", 0)
            d = domain_from_url(url)
            if d:
                from urllib.parse import urlparse
                p = urlparse(url)
                # Collapse long-hash segments to {hash} for stable descriptors.
                parts = [seg for seg in p.path.split("/") if seg]
                norm = "/" + "/".join(
                    "{hash}" if (len(seg) >= 16 and seg.isalnum()) else seg
                    for seg in parts
                ) if parts else "/"
                desc = f"xhr_replay:{method} {norm}"
                success = ok and 200 <= int(status or 0) < 400
                _playbook_record(
                    d, desc, "xhr_replay", success,
                    kind="xhr_replay", selector_pattern=url,
                )
        except Exception:
            pass

        return [TextContent(type="text", text=f"replay_xhr {method} {url}: {val}")]

    if name == "x_create_tweet":
        # End-to-end X post via reverse-engineered x-client-transaction-id.
        # Reads home HTML + ondemand JS from the tab context (cookies attach),
        # computes the body-bound signature via the vendored x_client_transaction
        # lib, then POSTs CreateTweet GraphQL from the same tab. Zero manual
        # seed, zero DOM typing — pure protocol-level send.
        tweet_text = arguments["text"]
        query_id = arguments.get("query_id") or ""
        reply_to = arguments.get("reply_to_tweet_id") or ""

        # Auto-route to an x.com / twitter.com tab when caller didn't pass one.
        # The session may have many tabs (IH, LinkedIn, HN…) and the generic
        # find_tab() fallback returns the first real tab — which won't have ct0.
        if not arguments.get("tab"):
            current_url = (tab.get("url") or "").lower()
            if "x.com" not in current_url and "twitter.com" not in current_url:
                x_tabs = [t for t in real_tabs(tabs)
                          if "x.com" in (t.get("url") or "").lower()
                          or "twitter.com" in (t.get("url") or "").lower()]
                if x_tabs:
                    tab = x_tabs[0]
                    ws_url = tab["webSocketDebuggerUrl"]
                else:
                    return [TextContent(type="text", text=f"x_create_tweet: no x.com tab open in this session. Open https://x.com/home and retry, or pass tab=<id>. Current tabs: {[t.get('url','') for t in real_tabs(tabs)][:5]}")]

        # 1. Pull home HTML + ondemand JS from Python directly (urllib).
        #    In-page fetch to x.com → abs.twimg.com is blocked by X's CSP
        #    (connect-src omits the CDN). The home HTML scaffold + ondemand JS
        #    are PUBLIC — no auth required, so a server-side fetch is fine and
        #    avoids the CORS surface entirely. Only ct0 + queryId still come
        #    from the tab (cookie + same-origin main bundle scrape).
        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        try:
            req = urllib.request.Request("https://x.com/home", headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            home_html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
        except Exception as e:
            return [TextContent(type="text", text=f"x_create_tweet: home fetch failed — {type(e).__name__}: {e}")]

        idx_match = re.search(r',(\d+):["\']ondemand\.s["\']', home_html)
        if not idx_match:
            return [TextContent(type="text", text="x_create_tweet: ondemand index not in home HTML — X may have changed the manifest layout.")]
        idx = idx_match.group(1)
        hash_match = re.search(r',' + idx + r':"([0-9a-f]+)"', home_html)
        if not hash_match:
            return [TextContent(type="text", text="x_create_tweet: ondemand hash not found in home HTML.")]
        ondemand_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_match.group(1)}a.js"
        try:
            req2 = urllib.request.Request(ondemand_url, headers={"User-Agent": UA})
            ondemand_js = urllib.request.urlopen(req2, timeout=10).read().decode("utf-8", errors="replace")
        except Exception as e:
            return [TextContent(type="text", text=f"x_create_tweet: ondemand fetch failed — {type(e).__name__}: {e}")]

        # ct0 + queryId scrape from the tab (same-origin, no CORS)
        tab_js = r"""(async function() {
            try {
                const ct0Match = document.cookie.split('; ').find(c => c.startsWith('ct0='));
                const ct0 = ct0Match ? ct0Match.split('=')[1] : '';
                let scrapedQid = '';
                try {
                    const mainScript = Array.from(document.scripts).map(s => s.src).find(s => /main\.[a-f0-9]+\.js/.test(s));
                    if (mainScript) {
                        const js = await fetch(mainScript).then(r => r.text());
                        const m = js.match(/queryId:"([^"]+)",operationName:"CreateTweet"/);
                        if (m) scrapedQid = m[1];
                    }
                } catch(e) {}
                return JSON.stringify({ct0, scrapedQid});
            } catch(e) {
                return JSON.stringify({error: 'tab read threw: ' + (e && e.message)});
            }
        })()"""
        r = await eval_in_tab(ws_url, tab_js)
        raw = r.get("result", {}).get("value", "{}")
        try:
            tab_data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            tab_data = {"error": "tab JSON parse failed"}
        if tab_data.get("error"):
            return [TextContent(type="text", text=f"x_create_tweet: {tab_data['error']}")]
        if not tab_data.get("ct0"):
            return [TextContent(type="text", text="x_create_tweet: ct0 cookie missing — not logged in to X?")]
        bundle = {"home": home_html, "ondemand": ondemand_js, "ct0": tab_data["ct0"], "scrapedQid": tab_data.get("scrapedQid", "")}

        # 2. Resolve queryId — arg > scraped > error
        if not query_id:
            query_id = bundle.get("scrapedQid") or ""
        if not query_id:
            return [TextContent(type="text", text="x_create_tweet: query_id missing — could not scrape from page bundle. Pass it explicitly (extract from any prior captured CreateTweet URL).")]

        # 3. Compute transaction-id with vendored lib
        try:
            import sys as _s
            _vendor_dir = str(Path(__file__).parent)
            if _vendor_dir not in _s.path:
                _s.path.insert(0, _vendor_dir)
            import bs4  # noqa: F401
            from vendor.x_client_transaction.transaction import ClientTransaction
            home_soup = bs4.BeautifulSoup(bundle["home"], "html.parser")
            ct = ClientTransaction(home_soup, bundle["ondemand"])
            path = f"/i/api/graphql/{query_id}/CreateTweet"
            transaction_id = ct.generate_transaction_id("POST", path)
        except ImportError as e:
            return [TextContent(type="text", text=f"x_create_tweet: missing dep ({e}). Run: pip install beautifulsoup4")]
        except Exception as e:
            return [TextContent(type="text", text=f"x_create_tweet: transaction-id compute failed — {type(e).__name__}: {e}")]

        # 4. Build CreateTweet body
        variables: dict = {
            "tweet_text": tweet_text,
            "dark_request": False,
            "media": {"media_entities": [], "possibly_sensitive": False},
            "semantic_annotation_ids": [],
            "disallowed_reply_options": None,
        }
        if reply_to:
            variables["reply"] = {"in_reply_to_tweet_id": reply_to, "exclude_reply_user_ids": []}
        body = {
            "variables": variables,
            "features": {
                "premium_content_api_read_enabled": False,
                "communities_web_enable_tweet_community_results_fetch": True,
                "c9s_tweet_anatomy_moderator_badge_enabled": True,
                "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
                "responsive_web_grok_analyze_post_followups_enabled": True,
                "responsive_web_jetfuel_frame": False,
                "responsive_web_grok_share_attachment_enabled": True,
                "responsive_web_edit_tweet_api_enabled": True,
                "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                "view_counts_everywhere_api_enabled": True,
                "longform_notetweets_consumption_enabled": True,
                "responsive_web_twitter_article_tweet_consumption_enabled": True,
                "tweet_awards_web_tipping_enabled": False,
                "responsive_web_grok_show_grok_translated_post": False,
                "responsive_web_grok_analysis_button_from_backend": True,
                "creator_subscriptions_quote_tweet_preview_enabled": False,
                "longform_notetweets_rich_text_read_enabled": True,
                "longform_notetweets_inline_media_enabled": True,
                "profile_label_improvements_pcf_label_in_post_enabled": True,
                "rweb_tipjar_consumption_enabled": True,
                "verified_phone_label_enabled": False,
                "articles_preview_enabled": True,
                "rweb_video_timestamps_enabled": True,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "freedom_of_speech_not_reach_fetch_enabled": True,
                "standardized_nudges_misinfo": True,
                "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                "responsive_web_grok_image_annotation_enabled": True,
                "responsive_web_graphql_timeline_navigation_enabled": True,
                "responsive_web_enhance_cards_enabled": False,
            },
            "queryId": query_id,
        }
        # X's public bearer — hardcoded in their bundle, stable for years.
        PUBLIC_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
        headers = {
            "authorization": f"Bearer {PUBLIC_BEARER}",
            "x-csrf-token": bundle["ct0"],
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "x-client-transaction-id": transaction_id,
            "content-type": "application/json",
        }

        # 5. Fire from tab context — cookies auto-attach
        post_js = (
            "(async function(p, h, b) { try {"
            "  const res = await fetch(p, {method:'POST', credentials:'include', headers:h, body:b});"
            "  const text = await res.text();"
            "  let parsed=null, tid=null;"
            "  try { parsed = JSON.parse(text); tid = parsed && parsed.data && parsed.data.create_tweet && parsed.data.create_tweet.tweet_results && parsed.data.create_tweet.tweet_results.result && parsed.data.create_tweet.tweet_results.result.rest_id; } catch(e) {}"
            "  return JSON.stringify({status: res.status, ok: res.ok, body: (text||'').slice(0,2000), tweet_id: tid});"
            "} catch(e) { return JSON.stringify({error: 'post threw: ' + (e && e.message)}); } })("
            + json.dumps(path) + "," + json.dumps(headers) + "," + json.dumps(json.dumps(body)) + ")"
        )
        r = await eval_in_tab(ws_url, post_js)
        raw = r.get("result", {}).get("value", "{}")
        try:
            res = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            res = {"error": "post JSON parse failed", "raw": raw[:300]}

        if res.get("ok") and res.get("tweet_id"):
            out = {
                "ok": True,
                "tweet_id": res["tweet_id"],
                "url": f"https://x.com/i/web/status/{res['tweet_id']}",
                "transaction_id": transaction_id,
                "query_id": query_id,
            }
            try:
                _playbook_record("x.com", f"x_create_tweet", "transaction_id", True, kind="xhr_replay", selector_pattern=path)
            except Exception:
                pass
        else:
            out = {
                "ok": False,
                "status": res.get("status"),
                "body": (res.get("body") or res.get("error") or "")[:600],
                "transaction_id": transaction_id,
                "query_id": query_id,
                "hint": "If 403/401: queryId rotated, refresh from a captured CreateTweet URL. If 400: features schema shifted — patch the features dict from a fresh capture. If 'Could not authenticate you': bearer/ct0 mismatch — reload the X tab.",
            }
        return [TextContent(type="text", text=f"x_create_tweet: {json.dumps(out)}")]

    if name == "inject_on_new_document":
        # CDP Page.addScriptToEvaluateOnNewDocument — survives all navigations
        # in the tab for the lifetime of the connection. Critical for capturing
        # network or fingerprint data across multi-page user flows (e.g. X reply
        # workflow where the user must navigate to a target tweet, which kills
        # any page-injected interceptor). Cleans up nicely via remove_injected_script.
        conn = await _get_conn(ws_url)
        await conn.send("Page.enable")
        script = arguments["script"]
        result = await conn.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script, "runImmediately": True},
        )
        ident = result.get("identifier") if isinstance(result, dict) else None
        if not ident:
            # Some CDP versions wrap the result; be defensive.
            ident = (result or {}).get("result", {}).get("identifier") if isinstance(result, dict) else None
        # Also evaluate it once in the CURRENT document so existing tabs get it
        # without needing a reload. Future navigations re-run via the registered handler.
        try:
            await eval_in_tab(ws_url, script)
        except Exception:
            pass
        return [TextContent(type="text", text=json.dumps({
            "ok": True,
            "identifier": ident,
            "note": "Script is registered and will auto-inject on every future navigation. Also ran once in current document.",
        }, indent=2))]

    if name == "remove_injected_script":
        conn = await _get_conn(ws_url)
        ident = arguments["identifier"]
        try:
            await conn.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": ident})
            return [TextContent(type="text", text=json.dumps({"ok": True, "removed": ident}))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}))]

    if name == "xhr_upload":
        import base64, mimetypes
        url = arguments["url"]
        files = arguments.get("files", [])
        fields = arguments.get("fields", {}) or {}
        method = arguments.get("method", "POST")
        headers = arguments.get("headers", {}) or {}
        if not files:
            return [TextContent(type="text", text="xhr_upload: 'files' is empty.")]

        # Read + base64 file bytes server-side
        payloads = []
        total = 0
        for entry in files:
            p = Path(entry["path"])
            if not p.is_file():
                return [TextContent(type="text", text=f"xhr_upload: file not found: {p}")]
            data = p.read_bytes()
            total += len(data)
            if total > 25_000_000:
                return [TextContent(type="text", text="xhr_upload: >25MB combined; inline-injection cap.")]
            payloads.append({
                "field": entry["field"],
                "name": p.name,
                "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                "b64": base64.b64encode(data).decode("ascii"),
            })

        # Construct FormData in page-JS context and fetch() with credentials.
        # credentials:'include' attaches cookies → CSRF tokens flow naturally.
        # Returns full response status + body (truncated) so caller can verify success.
        js = f"""(async function(url, method, files, fields, headers) {{
            try {{
                const fd = new FormData();
                for (const [k, v] of Object.entries(fields)) fd.append(k, v);
                for (const f of files) {{
                    const bin = atob(f.b64);
                    const buf = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
                    fd.append(f.field, new File([buf], f.name, {{type: f.type}}));
                }}
                const res = await fetch(url, {{
                    method, credentials: 'include', headers, body: fd
                }});
                const ct = res.headers.get('content-type') || '';
                let body;
                try {{
                    if (ct.includes('json')) body = JSON.stringify(await res.json());
                    else body = await res.text();
                }} catch(e) {{ body = '<could not read body: ' + e.message + '>'; }}
                return JSON.stringify({{
                    ok: res.ok, status: res.status, statusText: res.statusText,
                    url: res.url, content_type: ct, body: (body || '').slice(0, 3000)
                }});
            }} catch (e) {{
                return JSON.stringify({{ok:false, error: 'fetch threw: ' + (e && e.message)}});
            }}
        }})({json.dumps(url)}, {json.dumps(method)}, {json.dumps(payloads)}, {json.dumps(fields)}, {json.dumps(headers)})"""
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        try:
            parsed = json.loads(val) if val else {}
            ok = parsed.get("ok") and 200 <= int(parsed.get("status", 0)) < 300
            if ok:
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                if tab_dom:
                    _playbook_record(tab_dom, f"xhr_upload:{method}:{url}", "xhr_replay", True,
                                     kind="upload", selector_pattern=url)
        except Exception:
            pass
        return [TextContent(type="text", text=f"xhr_upload {method} {url}: {val}")]

    if name == "react_force_change":
        sel = arguments["selector"]
        value = arguments["value"]
        js = """(function(sel, value) {
            const el = document.querySelector(sel);
            if (!el) return JSON.stringify({ok:false, error:'no element matches: '+sel});

            // Walk up to find a React fiber (React 16/17/18 all expose it via __reactFiber* / __reactInternalInstance*)
            const findFiberKey = (node) => {
                for (const k of Object.keys(node)) {
                    if (k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')) return k;
                }
                return null;
            };
            let target = el, fiberKey = findFiberKey(el);
            if (!fiberKey) {
                // Walk DOM ancestors
                let cur = el;
                while (cur && cur !== document.body) {
                    fiberKey = findFiberKey(cur);
                    if (fiberKey) { target = cur; break; }
                    cur = cur.parentElement;
                }
            }
            if (!fiberKey) {
                // Fallback: native setter + standard event
                const proto = (el instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, value); else el.value = value;
                el.dispatchEvent(new InputEvent('input', {bubbles:true, composed:true, data:value, inputType:'insertText'}));
                el.dispatchEvent(new Event('change', {bubbles:true, composed:true}));
                return JSON.stringify({ok:true, mode:'no_fiber_fallback', readback: el.value});
            }

            // Set the native value first so React sees the new value when its onChange reads e.target.value
            const proto = (el instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, value); else el.value = value;

            // Find the onChange handler by walking fiber.return chain — props might be on the host or a wrapping component
            let fiber = target[fiberKey];
            let onChange = null;
            let hops = 0;
            while (fiber && hops < 10) {
                const props = fiber.memoizedProps || fiber.pendingProps;
                if (props) {
                    if (typeof props.onChange === 'function') { onChange = props.onChange; break; }
                    if (typeof props.onInput  === 'function') { onChange = props.onInput;  break; }
                }
                fiber = fiber.return;
                hops++;
            }
            if (!onChange) {
                // Fallback: just fire DOM events after native-setter
                el.dispatchEvent(new InputEvent('input', {bubbles:true, composed:true, data:value, inputType:'insertText'}));
                el.dispatchEvent(new Event('change', {bubbles:true, composed:true}));
                return JSON.stringify({ok:true, mode:'fiber_found_no_onchange', readback: el.value, fiber_hops: hops});
            }
            // Synthetic event matching React's SyntheticEvent shape closely enough that .target.value works
            const syntheticEvent = {
                target: el, currentTarget: el, type: 'change',
                bubbles: true, cancelable: true, defaultPrevented: false,
                preventDefault: () => {}, stopPropagation: () => {},
                persist: () => {}, nativeEvent: new Event('change', {bubbles:true}),
            };
            try {
                onChange(syntheticEvent);
            } catch (e) {
                return JSON.stringify({ok:false, error:'onChange threw: '+(e && e.message), readback: el.value});
            }
            // Also fire native events for any non-React listeners
            el.dispatchEvent(new InputEvent('input', {bubbles:true, composed:true, data:value, inputType:'insertText'}));
            el.dispatchEvent(new Event('change', {bubbles:true, composed:true}));
            return JSON.stringify({ok:true, mode:'fiber_onchange', readback: el.value, fiber_hops: hops});
        })(""" + json.dumps(sel) + ", " + json.dumps(value) + ")"
        r = await eval_in_tab(ws_url, js)
        val = r.get("result", {}).get("value", "{}")
        try:
            parsed = json.loads(val) if val else {}
            if parsed.get("ok"):
                tab_url_r = await eval_in_tab(ws_url, "location.href")
                tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                if tab_dom:
                    _playbook_record(tab_dom, f"react_force_change:{sel}", "react_force_change", True,
                                     kind="fill", selector_pattern=sel)
        except Exception:
            pass
        return [TextContent(type="text", text=f"react_force_change on '{sel}': {val}")]

    if name == "key_press":
        key = arguments["key"]
        mods = arguments.get("modifiers", []) or []
        mod_mask = 0
        for m in mods:
            mod_mask |= {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}.get(m, 0)
        key_table = {
            "Enter":     {"key": "Enter",     "code": "Enter",     "vk": 13, "text": "\r"},
            "Tab":       {"key": "Tab",       "code": "Tab",       "vk": 9,  "text": "\t"},
            "Escape":    {"key": "Escape",    "code": "Escape",    "vk": 27},
            "Backspace": {"key": "Backspace", "code": "Backspace", "vk": 8},
            "Space":     {"key": " ",         "code": "Space",     "vk": 32, "text": " "},
            "ArrowUp":   {"key": "ArrowUp",   "code": "ArrowUp",   "vk": 38},
            "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
            "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
            "ArrowRight":{"key": "ArrowRight","code": "ArrowRight","vk": 39},
            "PageUp":    {"key": "PageUp",    "code": "PageUp",    "vk": 33},
            "PageDown":  {"key": "PageDown",  "code": "PageDown",  "vk": 34},
            "Home":      {"key": "Home",      "code": "Home",      "vk": 36},
            "End":       {"key": "End",       "code": "End",       "vk": 35},
        }
        info = key_table.get(key)
        if not info:
            return [TextContent(type="text", text=f"key_press: unknown key '{key}'. Supported: {list(key_table.keys())}")]
        base = {
            "key": info["key"],
            "code": info["code"],
            "windowsVirtualKeyCode": info["vk"],
            "nativeVirtualKeyCode": info["vk"],
            "modifiers": mod_mask,
        }
        await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": info["text"]} if "text" in info else {})})
        await cdp_send(ws_url, "Input.dispatchKeyEvent", {"type": "keyUp", **base})
        # Record key_press into playbook — Enter on a form submit / Ctrl+S save /
        # Escape on a modal can each be the "unlock" move for a flow.
        try:
            cur_url = await eval_in_tab(ws_url, "location.href")
            url_now = cur_url.get("result", {}).get("value", "")
            d = domain_from_url(url_now)
            if d:
                combo = "+".join(mods + [key]) if mods else key
                _playbook_record(d, f"key_press:{combo}", "cdp_key", True, kind="key_press")
        except Exception:
            pass
        return [TextContent(type="text", text=f"Pressed {key}{' + '+'+'.join(mods) if mods else ''}.")]

    if name == "scroll_tab":
        direction = arguments.get("direction", "down")
        amount = arguments.get("amount", 500)
        delta_x = {"left": -amount, "right": amount}.get(direction, 0)
        delta_y = {"down": amount, "up": -amount}.get(direction, 0)
        await cdp_send(ws_url, "Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": 760, "y": 400,
            "deltaX": delta_x,
            "deltaY": delta_y,
            "modifiers": 0,
        })
        try:
            tab_url_r = await eval_in_tab(ws_url, "location.href")
            tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
            if tab_dom:
                _playbook_record(tab_dom, f"scroll:{direction}:{amount}", "scroll_wheel", True,
                                 kind="scroll")
        except Exception:
            pass
        return [TextContent(type="text", text=f"Scrolled {direction} {amount}px")]

    if name == "close_tab":
        tab_id = tab["id"]
        await cdp_send(ws_url, "Target.closeTarget", {"targetId": tab_id})
        return [TextContent(type="text", text=f"Closed: {tab.get('title', tab_id)[:80]}")]

    if name == "wait_for":
        selector = arguments["selector"]
        timeout_ms = arguments.get("timeout_ms", 10000)
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        started = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            result = await eval_in_tab(ws_url, f"!!document.querySelector({json.dumps(selector)})")
            if result.get("result", {}).get("value"):
                elapsed = round(asyncio.get_event_loop().time() - started, 2)
                # Record this as a precondition: "wait for selector X (took ~Y sec)" — the
                # next run can replay with a known wait window, no guessing.
                try:
                    tab_url_r = await eval_in_tab(ws_url, "location.href")
                    tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
                    if tab_dom:
                        _playbook_record(tab_dom, f"wait_for:{selector}", "selector_appears", True,
                                         kind="precondition", selector_pattern=selector)
                        # Stamp the elapsed time into action_log as preconditions metadata
                        pb = _load_live_playbook_raw()
                        alog = pb.setdefault(tab_dom, {}).setdefault("action_log", {})
                        a = alog.get(f"wait_for:{selector}")
                        if a is not None:
                            samples = a.get("elapsed_samples") or []
                            samples.append(elapsed)
                            a["elapsed_samples"] = samples[-10:]
                            save_playbook_data(pb)
                except Exception:
                    pass
                return [TextContent(type="text", text=f"Found: {selector} after {elapsed}s")]
            await asyncio.sleep(0.5)
        # Record timeout failures too — buyers' agents learn "this selector doesn't always appear"
        try:
            tab_url_r = await eval_in_tab(ws_url, "location.href")
            tab_dom = domain_from_url(tab_url_r.get("result", {}).get("value", ""))
            if tab_dom:
                _playbook_record(tab_dom, f"wait_for:{selector}", "selector_appears", False,
                                 kind="precondition", selector_pattern=selector)
        except Exception:
            pass
        return [TextContent(type="text", text=f"Timeout: {selector} not found after {timeout_ms}ms")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "_lib"))
        import bulletproof
        bulletproof.protect("webloom")
    except Exception as _e:
        print(f"[webloom] bulletproof unavailable: {_e}", file=__import__('sys').stderr)
    asyncio.run(main())
