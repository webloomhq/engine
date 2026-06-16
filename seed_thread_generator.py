"""Seed Thread generator — sandbox crawl that emits starter Threads.

Spawns a headless Chrome, visits each site anonymously, detects framework,
scans visible interactive elements, probes a couple of generic clicks, and
emits a `.thread.json` with starter knowledge. Output lands in
~/.webloom/threads/ ready for marketplace upload (or copy elsewhere).

These Seed Threads are intentionally basic: starter knowledge to bootstrap
the marketplace with content. Hand-crafted Pro Threads (with workflow-level
knowledge) come from real usage.

Usage:
    python seed_thread_generator.py --sites sites.txt
    python seed_thread_generator.py --site github.com --site producthunt.com
"""
import argparse
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

import server  # noqa: E402


# NOTE: A historical FRAMEWORK_PROBE_JS module-level constant used to live
# here. It was never called — crawl_one() uses server.FRAMEWORK_DETECT_JS as
# the authoritative version. Removed 2026-06-14 after external review flagged
# it as drift.

# Generic visible-element scan reused from server.SCAN_AX_JS
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _wait_devtools(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                json.loads(r.read())
                return True
        except Exception:
            await asyncio.sleep(0.2)
    return False


async def _open_tab(port: int, url: str = "about:blank") -> str | None:
    res = await server.cdp_browser_send(port, "Target.createTarget", {"url": url})
    tid = res.get("targetId")
    if not tid:
        return None
    # Resolve ws URL
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as r:
        tabs = json.loads(r.read())
    for t in tabs:
        if t.get("id") == tid:
            return t["webSocketDebuggerUrl"]
    return None


def _normalize_site(s: str) -> str:
    """github.com -> https://github.com.  https://x.com/path -> https://x.com/path."""
    s = s.strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return "https://" + s


def _domain_from(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "").lower()
    except Exception:
        return url


async def crawl_one(port: int, url: str, wait_seconds: float = 4.0) -> dict | None:
    """Visit a single URL anonymously and return a starter Thread dict.

    Flow: open tab at about:blank → enable Network capture → navigate to target
    → wait for hydration → harvest captured network during page-load.
    """
    domain = _domain_from(url)
    ws = await _open_tab(port, "about:blank")
    if not ws:
        return {"domain": domain, "error": "open_tab failed", "source_url": url}

    # No observer install needed — performance.getEntriesByType('resource')
    # persists every network resource since navigation start.

    try:
        # NOW navigate to the real URL — PerformanceObserver auto-installs via the new-document script
        await server.cdp_send(ws, "Page.navigate", {"url": url})

        # Poll readyState then add buffer for SPA hydration + post-paint xhr
        for _ in range(int(wait_seconds * 10)):
            r = await server.eval_in_tab(ws, "document.readyState")
            if r.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(2.5)  # buffer for late-mounting + post-paint xhr

        # Harvest captured network from the page's Performance API.
        # getEntriesByType('resource') returns every resource since nav start.
        captured: list[dict] = []
        try:
            r = await server.eval_in_tab(ws, """JSON.stringify(performance.getEntriesByType('resource').map(e => ({
                url: e.name,
                type: e.initiatorType || 'unknown',
                duration: Math.round(e.duration || 0),
                transferSize: e.transferSize || 0,
            })))""")
            captured = json.loads(r.get("result", {}).get("value", "[]"))
        except Exception:
            pass

        # 1. Anti-bot check — short-circuit if hit a challenge
        anti = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
        try:
            anti_data = json.loads(anti.get("result", {}).get("value", "{}"))
        except Exception:
            anti_data = {"verdict": "unknown"}
        verdict = anti_data.get("verdict", "normal")

        # 2. Framework + page probe (use the engine's authoritative version)
        r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
        try:
            probe_raw = json.loads(r.get("result", {}).get("value", "{}"))
        except Exception:
            probe_raw = {}
        # Flatten for legacy notes building below
        probe = {
            "frameworks": probe_raw.get("frameworks", []),
            "title": (probe_raw.get("page") or {}).get("title", ""),
            "url": (probe_raw.get("page") or {}).get("url", ""),
            "ready": (probe_raw.get("page") or {}).get("ready", ""),
            **(probe_raw.get("indicators") or {}),
            "has_iframe_count": (probe_raw.get("indicators") or {}).get("iframe_count", 0),
        }

        # 3. AX scan
        r = await server.eval_in_tab(ws, server.SCAN_AX_JS)
        try:
            scan = json.loads(r.get("result", {}).get("value", "{}"))
        except Exception:
            scan = {}

        # 4. Filter the captured network into interesting endpoints.
        # PerformanceObserver doesn't expose HTTP method — we filter by
        # initiatorType (fetch/xhr/beacon = API-like) and URL patterns.
        endpoints = []
        for e in captured:
            u = (e.get("url") or "").lower()
            t = (e.get("type") or "").lower()
            if not u or u.startswith("data:") or u.startswith("blob:"):
                continue
            if any(x in u for x in (".png", ".jpg", ".jpeg", ".webp", ".css", ".woff", ".woff2", ".svg", ".ico", ".gif", ".mp4", ".webm", ".ttf", "google-analytics", "googletagmanager", "doubleclick", "sentry", "datadog", "segment.io", "hotjar", "intercom", "facebook.net/tr", "/gtm.", "cookieyes")):
                continue
            if t in ("fetch", "xmlhttprequest", "beacon"):
                endpoints.append(e)
            elif "/api/" in u or "/graphql" in u or "/v1/" in u or "/v2/" in u:
                endpoints.append(e)
        endpoints = endpoints[:40]

        # 3. Synthesize Thread
        frameworks = probe.get("frameworks", []) or []
        framework_label = frameworks[0] if frameworks else "vanilla"

        # Heuristic default strategy
        default_strategy = "cdp"  # neutral safest default
        notes = []
        quirks = {}

        if any("react" in f for f in frameworks):
            notes.append("React detected — controlled inputs should use react_force_change as fallback for hostile onChange wrappers.")
            quirks["react_controlled_inputs"] = "use react_force_change escape hatch if normal fill fails"
        if any("amazon-aui" in f for f in frameworks):
            notes.append("Amazon AUI detected — modal save / form submit may need aui_dispatch with 'a:click' event.")
            quirks["aui_modal_save"] = "use aui_dispatch instead of click"
            default_strategy = "js"
        if any("backbone" in f for f in frameworks):
            notes.append("Backbone detected — read-only inspection via backbone_inspect; dispatch may require eval_js + Backbone.history.")
        if any("radix" in f or "headlessui" in f for f in frameworks):
            notes.append("Radix/HeadlessUI dropdowns/menus listen for mousedown not click — engine handles this via Stage 1 actionability + CDP press sequence.")
        if "redux-global-store" in frameworks or "redux-devtools-installed" in frameworks:
            notes.append("Redux store likely accessible — try react_inspect_store / redux_dispatch for state-commit bypass.")
        if probe.get("has_label_wrapped_file"):
            notes.append("Has label-wrapped hidden <input type=file> — use upload_file Strategy D (inject_input_selector) for uploads.")
            quirks["uploads"] = "use Strategy D"
        if probe.get("has_drop_zone"):
            notes.append("Has explicit drop zone — Strategy C (drop_target_selector) may work for uploads.")
        if probe.get("has_iframe_count", 0) > 0:
            notes.append(f"Page contains {probe['has_iframe_count']} iframe(s) — some content may be cross-origin and uncrossable by default.")
        if probe.get("has_password_input"):
            notes.append("Page has a password input — login flows likely. Pair with auth_totp if site supports TOTP 2FA.")

        # Build selectors from AX scan — first ~30 visible elements
        ax_lines = scan.get("lines", [])[:30]

        # Surface anti-bot warning prominently
        if verdict != "normal":
            notes.insert(0, f"⚠️ Anti-bot detected during seed: {verdict}. This Thread was generated in headless mode and may be incomplete. Needs real Chrome with user fingerprint to refine.")

        thread = {
            "domain": domain,
            "name": f"{domain} Starter",
            "version": "0.1.0",
            "author": "WebLoom Seed Generator",
            "license": "cc-by",
            "tier": "starter",
            "source_url": url,
            "framework": framework_label,
            "frameworks_detected": frameworks,
            "anti_bot_verdict": verdict,
            "anti_bot_signals": anti_data.get("signals", []),
            "default_strategy": default_strategy,
            "notes": notes,
            "quirks": quirks,
            # ── Authored fields (named maps) — empty at seed time; an
            #    author/AI fills these in by interpreting the raw harvest
            #    below. Engine merge consumes them as first-class.
            "selectors": {},
            "endpoints": {},
            "actions": {},
            "helpers": {},
            # ── Raw harvest fields — first-class merge surface. Engine
            #    helpers consult these as hints when no named entry matches.
            "ax_snapshot": ax_lines,
            "captured_endpoints": endpoints,
            "page_indicators": {
                "has_password_input": probe.get("has_password_input", False),
                "has_file_input": probe.get("has_file_input", False),
                "has_label_wrapped_file": probe.get("has_label_wrapped_file", False),
                "has_drop_zone": probe.get("has_drop_zone", False),
                "iframe_count": probe.get("has_iframe_count", 0),
            },
            "created_at": int(time.time()),
            "created_by": "seed_thread_generator.py",
        }

        return thread
    except Exception as e:
        return {"domain": domain, "error": f"crawl failed: {e}", "source_url": url}
    finally:
        # Close the tab to free resources
        try:
            await server.cdp_browser_send(port, "Target.closeTarget", {"targetId": ws.split("/")[-1]})
        except Exception:
            pass


async def main_async(args):
    sites: list[str] = []
    if args.sites:
        for line in Path(args.sites).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Inline tags like '#anti-bot' or '#api-native' after the domain
            # are honored by default: skip anti-bot (would yield empty Thread)
            # and skip api-native (better served by official SDK).
            domain_part = line.split("#")[0].strip()
            tags = {t.strip() for t in line.split("#")[1:] if t.strip()}
            if not domain_part:
                continue
            if "anti-bot" in tags and not args.include_anti_bot:
                print(f"[seed] skip {domain_part} (tag: anti-bot — needs real Chrome session)")
                continue
            if "api-native" in tags and not args.include_api_native:
                print(f"[seed] skip {domain_part} (tag: api-native — use their SDK)")
                continue
            sites.append(_normalize_site(domain_part))
    for s in args.site or []:
        sites.append(_normalize_site(s))
    sites = [s for s in sites if s]
    if not sites:
        print("No sites provided. Use --site or --sites <file>.")
        return 1

    out_dir = Path(args.out_dir) if args.out_dir else (Path.home() / ".webloom" / "threads")
    out_dir.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    udir = tempfile.mkdtemp(prefix="webloom-seed-")
    print(f"[seed] launching headless Chrome on port {port}")
    chrome_args = [
        server.CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udir}",
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        "--window-size=1280,800",
        "about:blank",
    ]
    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if not await _wait_devtools(port):
        print("[seed] Chrome did not start.")
        proc.terminate()
        return 1
    skipped = 0
    try:
        for url in sites:
            domain = _domain_from(url)
            out_path = out_dir / f"{domain}.thread.json"
            if args.skip_existing and out_path.exists():
                skipped += 1
                print(f"[seed] skip {domain} (already exists)")
                continue
            print(f"[seed] crawling {domain} ...")
            t = await crawl_one(port, url, wait_seconds=args.wait_seconds)
            if t is None or "error" in t:
                print(f"  failed: {t.get('error') if t else 'unknown'}")
                continue
            out_path.write_text(json.dumps(t, indent=2), encoding="utf-8")
            print(f"  wrote {out_path}  framework={t['framework']}  notes={len(t['notes'])}")
        if skipped:
            print(f"[seed] {skipped} domain(s) skipped (--skip-existing)")
    finally:
        try:
            for u, conn in list(server._cdp_pool.items()):
                await conn.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
    return 0


def main():
    p = argparse.ArgumentParser(description="WebLoom Seed Thread Generator")
    p.add_argument("--site", action="append", help="Site to crawl (repeatable). e.g. --site github.com --site producthunt.com")
    p.add_argument("--sites", help="Path to a text file with one site per line")
    p.add_argument("--out-dir", help="Output directory (default: ~/.webloom/threads)")
    p.add_argument("--wait-seconds", type=float, default=4.0, help="Max seconds to wait for each page to load")
    p.add_argument("--skip-existing", action="store_true", help="Skip domains that already have a .thread.json in the output dir. Lets you resume after a crash without re-crawling completed sites.")
    p.add_argument("--include-anti-bot", action="store_true", help="Include sites tagged '#anti-bot' in the seed list. Off by default — these yield near-empty Threads in headless and need a real Chrome session.")
    p.add_argument("--include-api-native", action="store_true", help="Include sites tagged '#api-native'. Off by default — these are better served by the site's official SDK.")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
