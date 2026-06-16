#!/usr/bin/env python3
"""
Chrome MCP — browser automation via Chrome DevTools Protocol.
Controls real Chrome instances: tabs, clicks, forms, JS, screenshots, playbooks.

Standalone FastMCP server — wraps chrome-mcp helpers.
Run: python server_fastmcp.py
"""

import json
import asyncio
import subprocess
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Import helpers from the original server. NOTE: real_cursor_click and
# find_and_focus_chrome_window are intentionally NOT imported here. The
# locked spec rule is "No Layer 3 / pyautogui, ever" (WEBLOOM_SPEC.md §15).
# server.py already disables that path on the canonical entrypoint; this
# alternate FastMCP entrypoint enforces the same boundary.
from server import (
    get_tabs, find_tab, eval_in_tab, screenshot_tab,
    cdp_send, cdp_real_click, get_element_center, get_exact_screen_coords,
    load_sessions, resolve_session, load_playbook, save_playbook_data, domain_from_url,
    CLICK_JS, FILL_JS, READ_JS, SCAN_JS, CHROME_EXE,
)

mcp = FastMCP("chrome-mcp")


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_sessions() -> str:
    """List all configured Chrome sessions and whether they are live."""
    sessions = load_sessions()
    lines = []
    any_live = False
    for sname, cfg in sessions.items():
        tabs = get_tabs(cfg["port"])
        if tabs:
            status = f"LIVE — {len(tabs)} tab{'s' if len(tabs) != 1 else ''}"
            any_live = True
        else:
            status = "offline"
        lines.append(f"{sname} (port {cfg['port']}): {status} — {cfg['description']}")
    if not any_live:
        lines.append("\nNo sessions running. Use launch_session to start one.")
    return "\n".join(lines)


@mcp.tool()
def launch_session(session: str, url: str = "") -> str:
    """Launch a Chrome session with its debug port. Use when a session is offline."""
    sessions = load_sessions()
    if session not in sessions:
        return f"Unknown session '{session}'. Available: {list(sessions.keys())}"
    cfg = sessions[session]
    port = cfg["port"]
    if get_tabs(port):
        return f"'{session}' is already running on port {port}."
    Path(cfg["user_data_dir"]).mkdir(parents=True, exist_ok=True)
    args = [CHROME_EXE, f"--remote-debugging-port={port}", f"--user-data-dir={cfg['user_data_dir']}", "--no-first-run", "--no-default-browser-check"]
    if url:
        args.append(url)
    subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    import time; time.sleep(2)
    tabs = get_tabs(port)
    if tabs:
        return f"Launched '{session}' on port {port}. Tabs: {', '.join(t.get('title','(loading)') for t in tabs[:3])}"
    return f"Chrome launched for '{session}' — still starting up. Try list_sessions in a moment."


@mcp.tool()
def list_tabs(session: str) -> str:
    """List all open tabs in a Chrome session."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline. Run launch_session first."
    return "\n\n".join(f"{i}. {t.get('title','(no title)')}\n   {t.get('url','')}\n   id: {t['id']}" for i, t in enumerate(tabs))


@mcp.tool()
async def read_tab(session: str, tab: str = "") -> str:
    """Read the text content and form fields of a Chrome tab."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    result = await eval_in_tab(ws_url, READ_JS)
    try:
        data = json.loads(result.get("result", {}).get("value", "{}"))
        out = f"{data.get('title')}\n{data.get('url')}\n\n{data.get('text','')}"
        if data.get("inputs"):
            out += f"\n\nForm fields:\n{data['inputs']}"
        return out
    except Exception as e:
        return f"Error reading tab: {e}"


@mcp.tool()
async def screenshot(session: str, tab: str = "") -> str:
    """Take a screenshot of a Chrome tab. Returns base64 JPEG."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    data = await screenshot_tab(ws_url)
    if data:
        return f"data:image/jpeg;base64,{data}"
    return "Screenshot failed."


@mcp.tool()
async def click(session: str, description: str, tab: str = "") -> str:
    """Click an element in a Chrome tab by text, aria-label, or CSS selector."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    js = CLICK_JS.replace("DESCRIPTION", json.dumps(description))
    result = await eval_in_tab(ws_url, js)
    val = result.get("result", {}).get("value", "no result")
    if "not found" not in val:
        return val
    coords = await get_element_center(ws_url, description)
    if coords:
        await cdp_real_click(ws_url, coords[0], coords[1])
        await asyncio.sleep(0.3)
        return f"clicked (CDP) '{description}'"
    # NEVER fall through to OS-level cursor movement. WebLoom does not move
    # the physical mouse — see WEBLOOM_SPEC.md §15. If CDP cannot click it,
    # surface the failure so the caller can escalate via react_invoke_handler
    # or hand off to the user.
    return f"not found via CDP: '{description}' — escalate to react_invoke_handler / vision / human"


@mcp.tool()
async def fill(session: str, fields: dict, tab: str = "") -> str:
    """Fill form fields in a Chrome tab. Pass {label_or_name: value} pairs."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    js = FILL_JS.replace("FIELDS", json.dumps(fields))
    result = await eval_in_tab(ws_url, js)
    return result.get("result", {}).get("value", "no result")


@mcp.tool()
async def eval_js(session: str, code: str, tab: str = "") -> str:
    """Run JavaScript in a Chrome tab and return the result."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    result = await eval_in_tab(ws_url, code)
    return json.dumps(result.get("result", {}), indent=2)


@mcp.tool()
async def navigate(session: str, url: str, tab: str = "") -> str:
    """Navigate a Chrome tab to a URL."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    await cdp_send(ws_url, "Page.navigate", {"url": url})
    await asyncio.sleep(1)
    return f"Navigated to {url}"


@mcp.tool()
def new_tab(session: str, url: str = "about:blank") -> str:
    """Open a new tab in a Chrome session."""
    port = resolve_session(session)
    with urllib.request.urlopen(f"http://localhost:{port}/json/new?{url}", timeout=5) as r:
        tab_info = json.loads(r.read())
    return f"Opened tab: {tab_info.get('id')} — {url}"


@mcp.tool()
async def scan_tab(session: str, tab: str = "", save_to_playbook: bool = True) -> str:
    """Full DOM scan — extracts every button, input, form, and selector from the live page."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    result = await eval_in_tab(ws_url, SCAN_JS)
    try:
        data = json.loads(result.get("result", {}).get("value", "{}"))
    except Exception:
        return "Scan failed — page may still be loading."
    url_str = data.get("url", "")
    domain = domain_from_url(url_str)
    lines = [f"Page Scan: {data.get('title')}", f"URL: {url_str}", ""]
    if data.get("inputs"):
        lines.append("Form Fields:")
        for inp in data["inputs"]:
            req = " (required)" if inp.get("required") else ""
            lines.append(f"  {inp['label'] or inp['name']} — type={inp['type']} selector={inp['selector']}{req}")
        lines.append("")
    if data.get("buttons"):
        lines.append("Buttons & Links:")
        for b in data["buttons"][:30]:
            lines.append(f"  [{b['tag']}] {b['text']} → {b['selector']}" + (f" href={b['href']}" if b.get("href") else ""))
        lines.append("")
    if save_to_playbook and domain and data.get("inputs"):
        playbook = load_playbook()
        if domain not in playbook:
            playbook[domain] = {}
        playbook[domain]["last_scan"] = {
            "url": url_str, "title": data.get("title"),
            "inputs": [{k: v for k, v in i.items() if k != "value"} for i in data.get("inputs", [])],
            "buttons": [{"text": b["text"], "selector": b["selector"]} for b in data.get("buttons", [])[:20]],
        }
        save_playbook_data(playbook)
        lines.append(f"Saved to playbook for {domain}")
    return "\n".join(lines)


@mcp.tool()
def get_playbook(domain: str = "") -> str:
    """Read everything Claude has learned about a domain — field names, quirks, working patterns."""
    playbook = load_playbook()
    if not domain:
        if not playbook:
            return "Playbook is empty. Run scan_tab on any site to start building it."
        return f"Known domains ({len(playbook)}):\n" + "\n".join(f"  - {d}" for d in playbook)
    if domain not in playbook:
        return f"No playbook entry for {domain} yet. Run scan_tab on that site first."
    return f"Playbook: {domain}\n\n{json.dumps(playbook[domain], indent=2)}"


@mcp.tool()
def save_playbook(domain: str, key: str, value: str) -> str:
    """Save a discovered pattern or working recipe for a domain so Claude remembers it."""
    domain = domain.replace("www.", "")
    playbook = load_playbook()
    if domain not in playbook:
        playbook[domain] = {}
    try:
        playbook[domain][key] = json.loads(value)
    except Exception:
        playbook[domain][key] = value
    save_playbook_data(playbook)
    return f"Saved '{key}' for {domain}."


@mcp.tool()
async def wait_for(session: str, selector: str, tab: str = "", timeout_ms: int = 10000) -> str:
    """Wait for a CSS selector to appear in a Chrome tab (polls every 500ms)."""
    port = resolve_session(session)
    tabs = get_tabs(port)
    if not tabs:
        return f"Session '{session}' is offline."
    ws_url = find_tab(tabs, tab)["webSocketDebuggerUrl"]
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        result = await eval_in_tab(ws_url, f"!!document.querySelector({json.dumps(selector)})")
        if result.get("result", {}).get("value"):
            return f"Found: {selector}"
        await asyncio.sleep(0.5)
    return f"Timeout: {selector} not found after {timeout_ms}ms"


if __name__ == "__main__":
    import os, uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
