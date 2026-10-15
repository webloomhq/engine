"""Regression tests for typing, click-by-text, JS waits, Thread recipes and cookie import.

Runs against local test pages in a throwaway headless Chrome (own port, own temp profile), through
the real tool handlers. Nothing touches your sessions, playbook or installed Threads: every path the
engine writes to is pointed at a temp folder before the server module is imported.

    python test_input_click.py            # exit code 0 = all passed

Covers:
  * key_type keystrokes mode types every printable character (dots used to vanish: "." went out
    with key code 46, which Chrome treats as Delete), keeps Enter and Tab as real keys, and inserts
    characters that have no US keyboard key (accents, emoji) as text.
  * click by visible text picks the innermost exact match, not the first wrapper in page order
    (an account chooser used to get the entry in the middle of the list), prefers a real control
    over plain text and what a click can reach over what an overlay covers, and refuses to guess
    between true ties (nth picks one).
  * wait_for js treats a returned DOM node as true (it comes back as {} which is falsy in Python).
  * click element_js + until_js retries a click that a still-hydrating page ignored.
  * replay_recipe runs a Thread's session_recipe and returns one JSON object.
  * import_profile sets __Host- cookies (by url, no domain) and keeps empty values.
"""
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="backslashreplace")  # a Windows console cannot print emoji
except Exception:
    pass

HERE = Path(__file__).parent
TMP =Path(tempfile.mkdtemp(prefix="webloom-test-"))
SESSION = "wltest"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _chrome_binary() -> str:
    env = os.environ.get("CHROME_EXE")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(p).exists():
            return p
    raise SystemExit("no Chrome/Chromium found (set CHROME_EXE)")


PORT = _free_port()
(TMP / "sessions.json").write_text(json.dumps({SESSION: {"port": PORT, "description": "test"}}), encoding="utf-8")
(TMP / "threads").mkdir()
os.environ.update({
    "CHROME_SESSIONS": str(TMP / "sessions.json"),
    "WEBLOOM_PLAYBOOK": str(TMP / "playbook.json"),
    "WEBLOOM_THREADS": str(TMP / "threads"),
    "WEBLOOM_AUTOPILOT": "1",
    "WEBLOOM_TELEMETRY": "off",
    "WEBLOOM_AUTO_RECORD": "off",
})
sys.path.insert(0, str(HERE))
import server  # noqa: E402


TYPE_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>typing</title></head><body>
<form id="f" onsubmit="event.preventDefault(); document.title = 'submitted:' + document.getElementById('t').value;">
  <input id="t" autocomplete="off" style="width:600px">
  <input id="u" autocomplete="off">
  <button type="submit">Go</button>
</form>
<textarea id="area"></textarea>
<script>
  window.keys = [];
  document.addEventListener('keydown', (e) => window.keys.push([e.key, e.code, e.keyCode]));
</script>
</body></html>"""

CLICK_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>clicks</title>
<style>
  body { font: 16px sans-serif; margin: 0; }
  .acct { display: block; padding: 14px; border-bottom: 1px solid #ccc; cursor: pointer; }
  #overlay { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: none; }
  #dlg { position: fixed; top: 80px; left: 80px; width: 400px; background: #fff; padding: 20px; display: none; }
  #log { position: fixed; right: 0; bottom: 0; }
</style></head><body>
<div id="wrap">
  <h2>Choose an account</h2>
  <ul id="accounts" style="list-style:none; padding:0; margin:0">
    <li><div class="acct" role="link" tabindex="0" onclick="hit('acct:Riley Park Studio')"><div>Riley Park Studio</div><div>studio@example.com</div></div></li>
    <li><div class="acct" role="link" tabindex="0" onclick="hit('acct:Jordan Lee Studio')"><div>Jordan Lee Studio</div><div>jl.studio@example.com</div></div></li>
    <li><div class="acct" role="link" tabindex="0" onclick="hit('acct:Riley Park')"><div>Riley Park</div><div>riley@example.com</div></div></li>
    <li><div class="acct" role="link" tabindex="0" onclick="hit('acct:Sam Jordan Lee')"><div>Sam Jordan Lee</div><div>sam@example.com</div></div></li>
    <li><div class="acct" role="link" tabindex="0" onclick="hit('acct:Jordan Lee')"><div>Jordan Lee</div><div>jordan@example.com</div></div></li>
  </ul>
  <p>
    <button onclick="hit('btn:Continue with email')">Continue with email</button>
    <button onclick="hit('btn:Continue')">Continue</button>
  </p>
  <p><button onclick="hit('btn:Delete 1')">Delete</button> <button onclick="hit('btn:Delete 2')">Delete</button></p>
  <p><button style="display:none" onclick="hit('btn:Save hidden')">Save</button> <button onclick="hit('btn:Save visible')">Save</button></p>
  <p><span>Open dialog</span> <button onclick="openDlg()">Open dialog</button></p>
  <p><button id="hydrating" onclick="hydrated ? openDlg() : (hydrated = true)">Start</button></p>
  <button onclick="hit('btn:Submit clip page')">Submit clip</button>
</div>
<div id="overlay"></div>
<div id="dlg" role="dialog"><h3>Submit clip</h3><input type="url" placeholder="https://example.com/video">
  <button onclick="hit('btn:Submit clip dialog')">Submit clip</button></div>
<div id="log"></div>
<script>
  window.hits = []; window.hydrated = false;
  function hit(x) { window.hits.push(x); document.getElementById('log').textContent = window.hits.join(' | '); }
  function openDlg() { document.getElementById('overlay').style.display = 'block'; document.getElementById('dlg').style.display = 'block'; hit('dialog:open'); }
  setTimeout(() => { const d = document.createElement('div'); d.id = 'late'; d.textContent = 'late'; document.body.appendChild(d); }, 1200);
</script>
</body></html>"""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


async def _tool(name: str, **args) -> str:
    args.setdefault("session", SESSION)
    out = await server._execute_tool_action(name, args)
    return server._first_text(out)


async def _js(ws: str, expr: str):
    r = await server.eval_in_tab(ws, expr)
    return (r.get("result") or {}).get("value")


async def _open(html: str) -> tuple[str, str]:
    url = "data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode("ascii")
    res = await server.cdp_browser_send(PORT, "Target.createTarget", {"url": url})
    tid = res["targetId"]
    for _ in range(50):
        for t in json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3).read()):
            if t.get("id") == tid and t.get("url", "").startswith("data:"):
                ws = t["webSocketDebuggerUrl"]
                for _ in range(50):
                    if await _js(ws, "document.readyState") == "complete":
                        return tid, ws
                    await asyncio.sleep(0.1)
        await asyncio.sleep(0.1)
    raise RuntimeError("test tab did not open")


async def _close(tid: str):
    try:
        await server.cdp_browser_send(PORT, "Target.closeTarget", {"targetId": tid})
    except Exception:
        pass


async def test_typing():
    tid, ws = await _open(TYPE_PAGE)
    try:
        cases = [
            "a.b@c.com/x:y",
            "https://www.example.com/shorts/x.y-z_1?q=a&b=%20#frag",
            "Hello, World! It's 100% (ok) $5 #1 ^ * + = [] {} ; : \" < > ? | \\ ` ~",
            "café ñandú 🎉 日本",
        ]
        for text in cases:
            await _js(ws, "(() => { const t = document.getElementById('t'); t.value = ''; t.focus(); window.keys = []; return 1; })()")
            out = await _tool("key_type", tab=tid, text=text)
            got = await _js(ws, "document.getElementById('t').value")
            check(f"key_type keystrokes types {text[:28]!r}", got == text, f"got {got!r} ({out})")
        await _js(ws, "(() => { const t = document.getElementById('t'); t.value = ''; t.focus(); window.keys = []; return 1; })()")
        await _tool("key_type", tab=tid, text=".")
        keys = await _js(ws, "JSON.stringify(window.keys)")
        check("a dot is the Period key (code 190), not Delete (46)", json.loads(keys or "[]") == [[".", "Period", 190]], keys)

        await _js(ws, "(() => { const t = document.getElementById('t'); t.value = ''; t.focus(); return 1; })()")
        await _tool("key_type", tab=tid, text="go\n")
        title = await _js(ws, "document.title")
        check("Enter is a real key (submits the form)", title == "submitted:go", f"title={title!r}")

        await _js(ws, "(() => { ['t','u'].forEach(i => document.getElementById(i).value = ''); document.getElementById('t').focus(); return 1; })()")
        await _tool("key_type", tab=tid, text="one\ttwo")
        vals = await _js(ws, "document.getElementById('t').value + '|' + document.getElementById('u').value")
        check("Tab is a real key (moves the focus)", vals == "one|two", f"got {vals!r}")

        await _js(ws, "(() => { const a = document.getElementById('area'); a.value = ''; a.focus(); return 1; })()")
        await _tool("key_type", tab=tid, text="line 1\r\nline 2")
        area = await _js(ws, "document.getElementById('area').value")
        check("\\r\\n is one Enter in a textarea", area == "line 1\nline 2", f"got {area!r}")
    finally:
        await _close(tid)


async def test_click():
    tid, ws = await _open(CLICK_PAGE)

    async def hits():
        return json.loads(await _js(ws, "JSON.stringify(window.hits)") or "[]")

    async def reset():
        await _js(ws, "(() => { window.hits = []; document.getElementById('log').textContent = ''; return 1; })()")

    try:
        # Prove the page reproduces the old failure: the first element in page order whose text
        # contains the words is the wrapper, and its center sits on a different account.
        center = await _js(ws, """(() => { const q = 'jordan lee';
            const el = [...document.querySelectorAll('div, span, li')].find(e => e.textContent.toLowerCase().includes(q));
            const r = el.getBoundingClientRect(); const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return el.id + ' -> ' + (top.closest('li') ? top.closest('li').innerText.split('\\n')[0] : top.tagName); })()""")
        check("old first-match rule lands on another entry (repro)", "Jordan Lee" != (center or "").split(" -> ")[-1], center)

        await reset()
        out = await _tool("click", tab=tid, description="Jordan Lee")
        check("click 'Jordan Lee' picks the exact entry, not 'Jordan Lee Studio' or 'Sam Jordan Lee'",
              await hits() == ["acct:Jordan Lee"], f"{await hits()} / {out[:200]}")

        await reset()
        await _tool("click", tab=tid, description="riley park")
        check("click is case-insensitive and still exact-first", await hits() == ["acct:Riley Park"], str(await hits()))

        await reset()
        await _tool("click", tab=tid, description="Continue")
        check("exact 'Continue' beats 'Continue with email'", await hits() == ["btn:Continue"], str(await hits()))

        await reset()
        await _tool("click", tab=tid, description="Jordan Lee Stu")
        check("starts-with beats contains when there is no exact match", await hits() == ["acct:Jordan Lee Studio"], str(await hits()))

        await reset()
        out = await _tool("click", tab=tid, description="Delete")
        check("two identical visible buttons: reports ambiguity, clicks nothing",
              out.startswith("click failed: ambiguous") and await hits() == [], out[:300])
        check("ambiguity report lists both candidates", "1) <button>" in out and "2) <button>" in out, out[:300])

        await reset()
        await _tool("click", tab=tid, description="Delete", nth=2)
        check("nth=2 picks the second of the tie", await hits() == ["btn:Delete 2"], str(await hits()))

        await reset()
        await _tool("click", tab=tid, description="Save")
        check("a hidden duplicate does not make it ambiguous", await hits() == ["btn:Save visible"], str(await hits()))

        await reset()
        await _tool("click", tab=tid, description="Open dialog")
        check("a real control beats plain text with the same words", await hits() == ["dialog:open"], str(await hits()))

        await reset()
        await _tool("click", tab=tid, description="Submit clip")
        check("with a dialog open, the dialog's button wins over the covered page button and the dialog title",
              await hits() == ["btn:Submit clip dialog"], str(await hits()))

        # wait_for js: an element expression must count as true (it comes back as {} over CDP)
        out = json.loads(await _tool("wait_for", tab=tid, js="document.querySelector('#late')", timeout_ms=5000))
        check("wait_for js on an element ends when it appears", out.get("ok") is True and out.get("value") is True, str(out))
        out = json.loads(await _tool("wait_for", tab=tid, js="document.querySelector('#never')", timeout_ms=800))
        check("wait_for js times out cleanly", out.get("ok") is False, str(out))

        # element_js + until_js: the first click is ignored (page still hydrating), the retry works
        await _js(ws, "(() => { document.getElementById('overlay').style.display = 'none'; document.getElementById('dlg').style.display = 'none'; window.hydrated = false; window.hits = []; return 1; })()")
        out = json.loads(await _tool("click", tab=tid, element_js="document.getElementById('hydrating')",
                                     until_js="getComputedStyle(document.getElementById('dlg')).display !== 'none'",
                                     attempts=4, until_timeout_seconds=2))
        check("element_js + until_js retries a click the page ignored", out.get("ok") is True and out.get("clicks") == 2, str(out))
        await _js(ws, """(() => { window.trusted = null; document.querySelector('#dlg button')
            .addEventListener('click', (e) => { window.trusted = e.isTrusted; }, {once: true}); return 1; })()""")
        await _tool("click", tab=tid, element_js="[...document.querySelectorAll('[role=dialog] button')].pop()")
        trusted = await _js(ws, "window.trusted")
        check("element_js click is a real (isTrusted) mouse click on the element",
              trusted is True and "btn:Submit clip dialog" in await hits(), f"isTrusted={trusted} hits={await hits()}")
    finally:
        await _close(tid)


async def test_thread_recipe():
    tid, ws = await _open(CLICK_PAGE)
    try:
        thread = {
            "domain": "wltest.local",
            "version": "1.0.0",
            "session_recipes": [
                {"name": "read_state", "output": "json", "parameters": ["who"], "actions": [
                    {"tool": "wait_for", "args": {"js": "document.querySelector('#accounts')", "timeout_ms": 5000}},
                    {"tool": "sleep", "args": {"seconds": 0.2}},
                    {"tool": "eval_js", "returns": True,
                     "args": {"code": "({who: '{{who}}', accounts: document.querySelectorAll('.acct').length})"}},
                ]},
                {"name": "optional_miss", "output": "json", "actions": [
                    {"tool": "wait_for", "optional": True, "args": {"js": "document.querySelector('#nope')", "timeout_ms": 300}},
                    {"tool": "eval_js", "returns": True, "args": {"code": "'still ran'"}},
                ]},
                {"name": "hard_miss", "output": "json", "actions": [
                    {"tool": "wait_for", "args": {"js": "document.querySelector('#nope')", "timeout_ms": 300}},
                    {"tool": "eval_js", "returns": True, "args": {"code": "'should not run'"}},
                ]},
            ],
        }
        (TMP / "threads" / "wltest.local.thread.json").write_text(json.dumps(thread), encoding="utf-8")

        async def replay(name, **params):
            out = await server._handle_recipe_tool("replay_recipe", {"name": name, "parameters": params,
                                                                      "session": SESSION, "tab": tid})
            return json.loads(server._first_text(out))

        r = await replay("wltest.local/read_state", who="tester")
        check("Thread recipe replays and returns its value as JSON",
              r.get("ok") is True and r.get("result") == {"who": "tester", "accounts": 5}, str(r)[:300])
        r = await replay("read_state", who="x")
        check("bare recipe name finds the only Thread that has it", r.get("ok") is True, str(r)[:200])
        r = await replay("wltest.local/read_state")
        check("missing parameter is an error, not a half run", r.get("ok") is False and "who" in r.get("error", ""), str(r)[:200])
        r = await replay("wltest.local/optional_miss")
        check("an optional failing step does not stop the run", r.get("ok") is True and r.get("result") == "still ran", str(r)[:200])
        r = await replay("wltest.local/hard_miss")
        check("a failing step stops the run with ok:false", r.get("ok") is False and r.get("failed_step") == 1, str(r)[:200])
        r = await replay("wltest.local/does_not_exist")
        check("unknown recipe is ok:false", r.get("ok") is False, str(r)[:200])
    finally:
        await _close(tid)


async def test_import_profile():
    cookies = [
        {"name": "__Host-csrf", "value": "", "domain": "example.com", "path": "/", "secure": True, "httpOnly": True,
         "sameSite": "lax", "expirationDate": time.time() + 86400, "hostOnly": True},
        {"name": "__Secure-session", "value": "abc", "domain": ".example.com", "path": "/", "secure": True,
         "sameSite": "no_restriction", "expirationDate": time.time() + 86400},
        {"name": "plain", "value": "1", "domain": ".example.com", "path": "/", "session": True},
    ]
    shaped = server._cookies_for_cdp(cookies)
    host = shaped[0]
    check("__Host- cookie goes by url with no domain", "domain" not in host and host.get("url") == "https://example.com/"
          and host.get("path") == "/" and host.get("secure") is True, str(host))
    check("empty cookie value is kept as \"\"", host.get("value") == "", str(host))
    check("sameSite no_restriction becomes None (and Secure)", shaped[1].get("sameSite") == "None" and shaped[1].get("secure") is True, str(shaped[1]))
    check("session cookie gets no expires", "expires" not in shaped[2], str(shaped[2]))
    f = TMP / "cookies.json"
    f.write_text(json.dumps(cookies), encoding="utf-8")
    out = json.loads(await _tool("import_profile", path=str(f)))
    check("import_profile sets all of them (bare list file, path alias)", out.get("ok") is True and out.get("imported") == 3, str(out))
    jar = (await server.cdp_browser_send(PORT, "Storage.getCookies")).get("cookies", [])
    names = {c["name"]: c for c in jar}
    check("__Host- cookie is in the jar as a host-only cookie", "__Host-csrf" in names and not names["__Host-csrf"]["domain"].startswith("."),
          str(names.get("__Host-csrf")))


async def main() -> int:
    udir = TMP / "profile"
    proc = subprocess.Popen(
        [_chrome_binary(), "--headless=new", f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1",
         f"--user-data-dir={udir}", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except Exception:
                await asyncio.sleep(0.1)
        print(f"[test] headless Chrome on port {PORT}, temp dir {TMP}")
        for t in (test_typing, test_click, test_thread_recipe, test_import_profile):
            print(f"\n{t.__name__}")
            try:
                await t()
            except Exception as e:
                check(f"{t.__name__} raised", False, f"{type(e).__name__}: {e}")
    finally:
        for conn in list(server._cdp_pool.values()):
            try:
                await conn.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n[test] {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
