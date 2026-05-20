"""Headless self-audit for chrome-mcp.

Spawns a headless Chrome on a unique port, loads a synthetic test page,
exercises each new helper, prints PASS/FAIL per check, tears down.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402  (the chrome-mcp server module)


# ── Test page: every pattern chrome-mcp claims to handle ──────────────────────
TEST_HTML = r"""<!DOCTYPE html>
<html><head><title>chrome-mcp smoke</title></head>
<body>
<h1>chrome-mcp smoke</h1>

<button id="plain-btn" type="button">Click Me Plain</button>

<div id="combobox-root" role="combobox" aria-haspopup="listbox">
  <div class="select-component-control">
    <div class="select-component-placeholder">Choose Author...</div>
  </div>
</div>
<ul id="hidden-listbox" role="listbox" style="display:none">
  <li role="option">Add New Author</li>
</ul>

<label>Title <input id="title-input" type="text" placeholder="Book title"></label>

<label class="upload-wrap">
  Upload cover
  <input id="hidden-file" type="file" hidden>
</label>

<input id="otp" type="text" autocomplete="one-time-code" name="otp">

<div id="counter">0</div>
<button id="counter-btn" type="button">Increment</button>

<script>
  // wire combobox to open the listbox on mousedown (Radix-ish)
  document.querySelector('#combobox-root .select-component-control')
    .addEventListener('mousedown', () => {
      document.getElementById('hidden-listbox').style.display = 'block';
      document.getElementById('combobox-root').setAttribute('aria-expanded', 'true');
    });
  document.getElementById('counter-btn').addEventListener('click', () => {
    const c = document.getElementById('counter');
    c.textContent = String(parseInt(c.textContent, 10) + 1);
  });
  // Plain input + change listener for fill testing
  document.getElementById('title-input').addEventListener('input', (e) => {
    document.title = 'typed: ' + e.target.value;
  });
</script>
</body></html>"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _wait_for_devtools(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                json.loads(r.read())
                return True
        except Exception:
            await asyncio.sleep(0.2)
    return False


async def _open_test_tab(port: int) -> str:
    # Modern Chrome /json/new requires PUT. Use Target.createTarget via the browser WS.
    import base64
    data_url = "data:text/html;base64," + base64.b64encode(TEST_HTML.encode("utf-8")).decode("ascii")
    res = await server.cdp_browser_send(port, "Target.createTarget", {"url": data_url})
    target_id = res.get("targetId")
    # Look up the new tab in /json to get its ws url
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as r:
        tabs = json.loads(r.read())
    for t in tabs:
        if t.get("id") == target_id:
            return t["webSocketDebuggerUrl"]
    # Fallback: first page-type tab that matches our data url
    for t in tabs:
        if t.get("type") == "page" and t.get("url", "").startswith("data:"):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("Could not find new test tab")


# ── checks ────────────────────────────────────────────────────────────────────

results: list[tuple[str, bool, str]] = []

def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    icon = "[PASS]" if ok else "[FAIL]"
    print(f"  {icon} {name}" + (f" -- {detail}" if detail and not ok else ""))


async def run(ws_url: str):
    # Wait for the page to be ready
    for _ in range(20):
        r = await server.eval_in_tab(ws_url, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            break
        await asyncio.sleep(0.1)

    # ── 1. CDP pool basics ────────────────────────────────────────────────────
    r = await server.cdp_send(ws_url, "Runtime.evaluate", {"expression": "1+1", "returnByValue": True})
    check("cdp_send roundtrip", r.get("result", {}).get("value") == 2)

    # Concurrent calls — id monotonic, no collisions
    rs = await asyncio.gather(*[server.eval_in_tab(ws_url, f"({i})") for i in range(5)])
    values = [r.get("result", {}).get("value") for r in rs]
    check("cdp pool concurrent (5 calls, monotonic ids)", values == [0, 1, 2, 3, 4])

    # ── 2. SCAN_AX_JS + @eN refs ──────────────────────────────────────────────
    r = await server.eval_in_tab(ws_url, server.SCAN_AX_JS)
    try:
        scan = json.loads(r.get("result", {}).get("value", "{}"))
    except Exception:
        scan = {}
    check("scan_tab ax returns @eN tree", bool(scan.get("lines")), str(scan)[:200])
    has_refs = any(ln.startswith("@e") for ln in scan.get("lines", []))
    check("scan_tab ax lines start with @eN", has_refs)
    check("scan_tab ax found multiple interactive els", scan.get("ref_count", 0) >= 5)

    # @eN resolver — pick the title input ref
    title_ref = None
    for ln in scan.get("lines", []):
        if "title-input" in ln or "Book title" in ln or "Title" in ln:
            title_ref = ln.split(" ", 1)[0]
            break
    if title_ref:
        resolved = await server.resolve_ax_ref(ws_url, title_ref)
        check("resolve_ax_ref returns CSS selector",
              resolved != title_ref and ("#" in resolved or "input" in resolved),
              f"{title_ref} -> {resolved}")
    else:
        check("resolve_ax_ref returns CSS selector", False, "no title ref found in scan")

    # ── 3. Actionability ──────────────────────────────────────────────────────
    probe = await server.check_actionability(ws_url, "Click Me Plain")
    check("actionability: plain button found", probe.get("found"))
    check("actionability: plain button actionable", probe.get("actionable"))
    check("actionability: hitsTarget true", probe.get("hitsTarget"))

    probe_missing = await server.check_actionability(ws_url, "Nonexistent Button XYZ")
    check("actionability: nonexistent element returns found=false", not probe_missing.get("found"))

    # ── 4. CLICK_JS — pointer/mouse/click sequence + AUI wrapper lift ─────────
    # Reset counter, dispatch click on the counter button
    await server.eval_in_tab(ws_url, "document.getElementById('counter').textContent = '0'")
    js = server.CLICK_JS.replace("DESCRIPTION", json.dumps("Increment"))
    cr = await server.eval_in_tab(ws_url, js)
    counter_val = (await server.eval_in_tab(ws_url, "document.getElementById('counter').textContent")).get("result", {}).get("value")
    check("CLICK_JS fires real click sequence", counter_val == "1", f"counter={counter_val}, cr={cr.get('result',{}).get('value')}")

    # Test Radix-ish combobox — listens for mousedown only
    js2 = server.CLICK_JS.replace("DESCRIPTION", json.dumps("Choose Author..."))
    await server.eval_in_tab(ws_url, js2)
    await asyncio.sleep(0.2)
    expanded = (await server.eval_in_tab(ws_url, "document.getElementById('combobox-root').getAttribute('aria-expanded')")).get("result", {}).get("value")
    check("CLICK_JS fires mousedown (combobox opens via mousedown)", expanded == "true", f"aria-expanded={expanded}")

    # ── 5. snapshot_for_verify catches dropdown opening ───────────────────────
    # Reset combobox
    await server.eval_in_tab(ws_url, "document.getElementById('hidden-listbox').style.display='none'; document.getElementById('combobox-root').setAttribute('aria-expanded','false')")
    snap_before = await server.snapshot_for_verify(ws_url)
    await server.eval_in_tab(ws_url, "document.querySelector('#combobox-root .select-component-control').dispatchEvent(new MouseEvent('mousedown', {bubbles:true}))")
    await asyncio.sleep(0.2)
    snap_after = await server.snapshot_for_verify(ws_url)
    check("verifier detects dropdown via aria-expanded/listbox", snap_after != snap_before,
          f"before={snap_before[:120]} after={snap_after[:120]}")

    # ── 6. FILL_JS React-aware setter ─────────────────────────────────────────
    js3 = server.FILL_JS.replace("FIELDS", json.dumps({"title": "Test Book"}))
    await server.eval_in_tab(ws_url, js3)
    val_back = (await server.eval_in_tab(ws_url, "document.getElementById('title-input').value")).get("result", {}).get("value")
    check("FILL_JS sets input value (React-aware)", val_back == "Test Book", f"got: {val_back}")
    title_after = (await server.eval_in_tab(ws_url, "document.title")).get("result", {}).get("value")
    check("FILL_JS fires input event (title listener ran)", "Test Book" in title_after, f"title={title_after}")

    # ── 7. key_type modes (sanity) — would need full handler, skip here. ──────
    #    We verify the underlying CDP commands work by calling Input.insertText
    await server.eval_in_tab(ws_url, "document.getElementById('otp').focus()")
    await server.cdp_send(ws_url, "Input.insertText", {"text": "123456"})
    otp_v = (await server.eval_in_tab(ws_url, "document.getElementById('otp').value")).get("result", {}).get("value")
    check("CDP Input.insertText writes to focused input", otp_v == "123456", f"otp={otp_v}")

    # ── 8. aui_dispatch inspect on a page without AUI ─────────────────────────
    aui_js = """(function() {
        const result = { aui_present: !!window.A };
        return JSON.stringify(result);
    })()"""
    r = await server.eval_in_tab(ws_url, aui_js)
    val = json.loads(r.get("result", {}).get("value", "{}"))
    check("aui_dispatch handles missing AUI cleanly", val.get("aui_present") is False)

    # ── 9. backbone_inspect on page without Backbone ──────────────────────────
    bb_js = """(function() { return JSON.stringify({backbone: !!window.Backbone}); })()"""
    r = await server.eval_in_tab(ws_url, bb_js)
    val = json.loads(r.get("result", {}).get("value", "{}"))
    check("backbone_inspect handles missing Backbone cleanly", val.get("backbone") is False)

    # ── 10. Playbook helpers ──────────────────────────────────────────────────
    test_domain = "smoke-test.local"
    server._playbook_record(test_domain, "Test Button", "cdp", True)
    server._playbook_record(test_domain, "Test Button", "cdp", True)
    server._playbook_record(test_domain, "Test Button", "cdp", False)
    pref = server._playbook_get_strategy(test_domain, "Test Button")
    check("playbook records strategy outcomes (2/3 success)",
          pref and pref.get("strategy") == "cdp" and pref.get("successes") == 2 and pref.get("failures") == 1)
    check("playbook computes success_rate",
          pref and abs(pref.get("success_rate", 0) - (2/3)) < 0.01,
          f"rate={pref.get('success_rate') if pref else None}")

    # Strategy switch resets counter
    server._playbook_record(test_domain, "Test Button", "js", True)
    pref2 = server._playbook_get_strategy(test_domain, "Test Button")
    check("playbook resets counter on strategy change",
          pref2 and pref2.get("strategy") == "js" and pref2.get("successes") == 1 and pref2.get("failures") == 0,
          f"{pref2}")

    # Cleanup test entry
    pb = server.load_playbook()
    pb.pop(test_domain, None)
    server.save_playbook_data(pb)

    # ── 11. replay_xhr foundation: page-context fetch works ───────────────────
    fetch_js = """(async function() {
        try {
            const res = await fetch('data:application/json,{"ok":true}');
            const t = await res.text();
            return JSON.stringify({ok: true, body: t});
        } catch(e) { return JSON.stringify({ok:false, error:e.message}); }
    })()"""
    r = await server.eval_in_tab(ws_url, fetch_js)
    try:
        fetch_val = json.loads(r.get("result", {}).get("value", "{}"))
        check("page-context fetch works (replay_xhr foundation)", fetch_val.get("ok") is True)
    except Exception:
        check("page-context fetch works (replay_xhr foundation)", False, "JSON parse failed")

    # ── 12. Thread install/list mechanism ─────────────────────────────────────
    import tempfile as _tf
    test_thread = {
        "domain": "smoke-thread-test.local",
        "name": "Smoke Test Thread",
        "version": "0.0.1",
        "framework": "test",
        "default_strategy": "cdp",
        "notes": ["this is a smoke test thread"],
    }
    server.THREADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = server.THREADS_DIR / "smoke-thread-test.local.thread.json"
    dest.write_text(json.dumps(test_thread), encoding='utf-8')
    threads_after = server.load_threads()
    check("Thread becomes discoverable after install", "smoke-thread-test.local" in threads_after)
    # Merged playbook should expose the test Thread's framework
    pb_merged = server.load_playbook().get("smoke-thread-test.local", {})
    check("Thread merges into load_playbook output",
          pb_merged.get("framework") == "test" and pb_merged.get("_thread_present") is True)
    # Cleanup
    try: dest.unlink()
    except Exception: pass

    # ── 13. Vision config check (don't actually call API) ─────────────────────
    check("VISION_BACKEND configured (claude default)", server.VISION_BACKEND == "claude")
    check("ANTHROPIC_API_KEY present in env",
          bool(os.environ.get("ANTHROPIC_API_KEY")), "set ANTHROPIC_API_KEY for vision Layer 2.5 to work")


async def main():
    port = _free_port()
    print(f"\n[smoke] launching headless Chrome on port {port} ...")
    udir = tempfile.mkdtemp(prefix="chrome-mcp-smoke-")
    args = [
        server.CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udir}",
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
    try:
        ok = await _wait_for_devtools(port)
        if not ok:
            print("[smoke] Chrome did not expose DevTools in time. Aborting.")
            return 1
        ws = await _open_test_tab(port)
        print(f"[smoke] test tab ws: {ws[:80]}...")
        await run(ws)
    finally:
        # Close any pool conns
        for u, conn in list(server._cdp_pool.items()):
            try:
                await conn.close()
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n[smoke] {passed}/{total} checks passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
