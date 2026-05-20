"""WebLoom Weaver — unified Thread builder + validator + recipe runner.

Three modes:
  discover <url>              Probe a site → write Thread + frozen pre-flight.
  check <domain>              Re-run the frozen probes. Green/red drift detector.
  apply <domain> <recipe>     Run a recipe against the Thread. Pre-flights first,
                              halts on first unexpected state. --dry-run to preview.

No daemon. Read-only by default — `apply` is the only mode that touches state,
and only with verified Thread + explicit recipe.

Usage:
  python weaver.py discover https://draft2digital.com/book/ --port 9226
  python weaver.py check draft2digital.com --port 9226
  python weaver.py apply upwork.com recipes/update_title.json --port 9226 --dry-run
  python weaver.py list

Threads live at ~/.webloom/threads/<domain>.thread.json and are the unit you
share / sell on the WebLoom Atelier. Every Thread ships with its own pre-flight
because the probes that discovered it ARE the probes that validate it.
"""
import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
SERVER_DIR = HERE.parent
sys.path.insert(0, str(SERVER_DIR))

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)
    except Exception:
        pass

import server  # noqa: E402

THREADS_DIR = Path.home() / ".webloom" / "threads"
THREADS_DIR.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


# ─── Web-search bootstrap (free, no API key) ──────────────────────────────────
def _ddg_search(query: str, n: int = 5) -> list[str]:
    """DuckDuckGo HTML scrape. Returns top result URLs."""
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore")
        hits = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html)
        cleaned = []
        for h in hits[:n * 2]:
            m = re.search(r"uddg=([^&]+)", h)
            cleaned.append(urllib.parse.unquote(m.group(1)) if m else h)
        return cleaned[:n]
    except Exception as e:
        print(f"    ⚠ web search failed: {e}")
        return []


def _fetch(url: str, max_bytes: int = 80_000) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(max_bytes).decode("utf-8", errors="ignore")
    except Exception:
        return ""


SELECTOR_PATTERNS = [
    re.compile(r"#[a-zA-Z][\w-]{3,40}"),          # #ids
    re.compile(r'\[name=[\'"]([^\'"]{2,60})[\'"]\]'),
    re.compile(r'\[data-[a-z-]+=[\'"][^\'"]{2,40}[\'"]\]'),
    re.compile(r'role=[\'"]([a-z]+)[\'"]'),
    re.compile(r'aria-label=[\'"]([^\'"]{2,40})[\'"]'),
]


def gather_priors(domain: str) -> dict:
    """Search the web for hints about this domain's DOM. Returns a 'prior'."""
    print(f"\n[priors] searching the web for hints about {domain}...")
    queries = [
        f'site:github.com {domain} selector OR querySelector',
        f'site:stackoverflow.com {domain} automation selenium OR puppeteer',
        f'{domain} form input name id',
    ]
    urls: list[str] = []
    for q in queries:
        urls.extend(_ddg_search(q, n=3))
        time.sleep(0.5)
    urls = list(dict.fromkeys(urls))[:8]

    selector_hits: dict[str, int] = {}
    sources: list[str] = []
    for u in urls:
        body = _fetch(u)
        if not body:
            continue
        sources.append(u)
        for pat in SELECTOR_PATTERNS:
            for m in pat.findall(body):
                key = m if isinstance(m, str) else m[0]
                if not key or len(key) < 3:
                    continue
                selector_hits[key] = selector_hits.get(key, 0) + 1

    top = sorted(selector_hits.items(), key=lambda x: -x[1])[:30]
    print(f"  fetched {len(sources)} pages · {len(top)} candidate selectors")
    return {"sources": sources, "candidate_selectors": [k for k, _ in top]}


# ─── CDP helpers ─────────────────────────────────────────────────────────────
def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


async def _pick_tab(port: int, domain_hint: str | None) -> str:
    tabs = _list_tabs(port)
    if domain_hint:
        for t in tabs:
            if t.get("type") == "page" and domain_hint in (t.get("url", "") or ""):
                return t["webSocketDebuggerUrl"]
    for t in tabs:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("no page tab found")


async def _navigate(ws: str, url: str, wait_seconds: float = 8.0):
    await server.cdp_send(ws, "Page.navigate", {"url": url})
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(2.0)


async def _eval(ws: str, js: str):
    r = await server.eval_in_tab(ws, js)
    return r.get("result", {}).get("value")


# ─── Probe primitives (each returns a check spec we can freeze) ──────────────
async def probe_framework(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
    try:
        data = json.loads(r.get("result", {}).get("value", "{}"))
        return {"frameworks": data.get("frameworks", []), "raw": data}
    except Exception:
        return {"frameworks": [], "raw": {}}


async def probe_inputs(ws: str) -> list[dict]:
    # Detect fill_strategy per element WITHOUT writing a value:
    #   lexical_set     — element or ancestor has __lexicalEditor (Lexical editor)
    #   contenteditable — element is contenteditable, no Lexical
    #   react_setter    — React-managed input (has __reactProps / __reactFiber key)
    #   fast_setter     — plain input/textarea
    #   select_option   — <select>
    js = r"""JSON.stringify(Array.from(document.querySelectorAll('input, textarea, select, [contenteditable="true"], [role=textbox], [role=combobox]')).filter(el => el.offsetParent !== null).slice(0,60).map(el => {
        const reactKey = Object.keys(el).find(k => k.startsWith('__reactProps') || k.startsWith('__reactFiber'));
        let strategy;
        if (el.tagName === 'SELECT') strategy = 'select_option';
        else if (el.__lexicalEditor || el.closest('[data-lexical-editor]') || el.closest('.editor-container [contenteditable]')) strategy = 'lexical_set';
        else if (el.isContentEditable) strategy = 'contenteditable';
        else if (reactKey) strategy = 'react_setter';
        else strategy = 'fast_setter';
        return {
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            name: el.name || null,
            type: el.type || null,
            placeholder: el.placeholder || null,
            aria_label: el.getAttribute('aria-label') || null,
            role: el.getAttribute('role') || null,
            content_editable: el.isContentEditable || false,
            fill_strategy: strategy
        };
    }))"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


async def probe_buttons(ws: str) -> list[dict]:
    # Detect click_strategy per element:
    #   aui_fire        — Amazon AUI: parent is .a-declarative with data-action
    #   radix_portal    — Radix/HeadlessUI: data-radix-* / data-headlessui-* markers
    #   react_handler   — has __reactProps with onClick
    #   js_dispatch     — default (pointer+mouse event sequence on leaf)
    js = r"""JSON.stringify(Array.from(document.querySelectorAll('button, [role=button], input[type=submit], a.btn, [class*=btn]')).filter(el => el.offsetParent !== null).slice(0,60).map(el => {
        const auiP = el.closest('.a-declarative[data-action]');
        const radixP = el.closest('[data-radix-collection-item], [data-state], [data-headlessui-state]');
        const reactKey = Object.keys(el).find(k => k.startsWith('__reactProps'));
        let strategy = 'js_dispatch';
        if (auiP) strategy = 'aui_fire';
        else if (radixP) strategy = 'radix_portal';
        else if (reactKey && el[reactKey]?.onClick) strategy = 'react_handler';
        return {
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            text: ((el.textContent || el.value || '').trim().slice(0,60)) || null,
            aria_label: el.getAttribute('aria-label') || null,
            click_strategy: strategy
        };
    }))"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


async def probe_edit_triggers(ws: str) -> list[dict]:
    """Find every clickable that probably opens an edit modal."""
    js = r"""JSON.stringify(Array.from(document.querySelectorAll('button, a, [role=button]')).filter(el => {
        if (el.offsetParent === null) return false;
        const t = ((el.textContent||'') + ' ' + (el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('data-cy')||'') + ' ' + (el.getAttribute('data-test')||'') + ' ' + (el.className||'')).toLowerCase();
        return /edit|pencil|modify|update profile/.test(t) && !/last edit|edited|history/.test(t);
    }).slice(0, 20).map((el, i) => {
        el.setAttribute('data-weaver-trigger', 'edit_' + i);
        const label = ((el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 50)) || ('edit_' + i);
        return { idx: i, label: label, selector: '[data-weaver-trigger="edit_' + i + '"]' };
    }))"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


async def probe_modal_inputs(ws: str) -> list[dict]:
    """Probe inputs scoped to the currently-open modal (if any)."""
    js = r"""(function(){
        const modal = document.querySelector('[role=dialog]:not([aria-hidden=true]), [aria-modal=true], .air3-modal, .up-modal, .modal.show, .ReactModal__Content');
        if (!modal) return JSON.stringify({modal_found: false, inputs: []});
        const inputs = Array.from(modal.querySelectorAll('input, textarea, select, [contenteditable="true"], [role=textbox], [role=combobox]')).filter(el => el.offsetParent !== null).slice(0, 30).map(el => {
            const reactKey = Object.keys(el).find(k => k.startsWith('__reactProps') || k.startsWith('__reactFiber'));
            let strategy;
            if (el.tagName === 'SELECT') strategy = 'select_option';
            else if (el.__lexicalEditor || el.closest('[data-lexical-editor]')) strategy = 'lexical_set';
            else if (el.isContentEditable) strategy = 'contenteditable';
            else if (reactKey) strategy = 'react_setter';
            else strategy = 'fast_setter';
            return {
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                name: el.name || null,
                type: el.type || null,
                placeholder: el.placeholder || null,
                aria_label: el.getAttribute('aria-label') || null,
                fill_strategy: strategy
            };
        });
        const buttons = Array.from(modal.querySelectorAll('button, [role=button]')).filter(el => el.offsetParent !== null).slice(0, 15).map(el => ({
            text: ((el.textContent || '').trim().slice(0,40)) || null,
            aria_label: el.getAttribute('aria-label') || null,
            id: el.id || null
        }));
        return JSON.stringify({modal_found: true, inputs: inputs, buttons: buttons, modal_title: (modal.querySelector('h1,h2,h3,[role=heading]')?.textContent || '').trim().slice(0, 80)});
    })()"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "{}")
    except Exception:
        return {"modal_found": False, "inputs": [], "buttons": []}


async def find_modal_urls(ws: str) -> list[dict]:
    """Find URL-routable modals (links with /modal- or ?modal in href)."""
    js = r"""JSON.stringify(Array.from(document.querySelectorAll('a[href]')).filter(a => {
        const h = a.getAttribute('href') || '';
        return /\/modal-|[?&]modal[a-z-]*=/i.test(h) && a.offsetParent !== null;
    }).slice(0, 30).map(a => ({
        href: a.href,
        label: (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 60) || a.href.split('/').pop()
    })))"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


async def _is_cf_challenge(ws: str) -> bool:
    js = r"""(function(){
        const t = (document.body.innerText || '').toLowerCase();
        return !!(document.querySelector('#challenge-form, iframe[src*="cloudflare"], iframe[src*="challenges.cloudflare"]') ||
                  /verify you are human|just a moment|checking your browser/.test(t));
    })()"""
    return bool(await _eval(ws, js))


async def walk_url_modals(ws: str, base_url: str, cautious: bool = True) -> list[dict]:
    """Walk URL-routable modals by direct navigation. Cautious by default."""
    inter_step = 2.8 if cautious else 0.4
    settle = 1.6 if cautious else 0.8
    print(f"\n[explore] scanning for URL-routable modals... (pacing={'cautious' if cautious else 'fast'})")
    urls = await find_modal_urls(ws)
    print(f"  found {len(urls)} modal URL(s)")
    if not urls:
        return []

    states = []
    seen = set()
    for u in urls:
        if u["href"] in seen:
            continue
        seen.add(u["href"])

        if await _is_cf_challenge(ws):
            print(f"  ⚠ Cloudflare challenge detected — halting walk to avoid escalation")
            break

        try:
            await _navigate(ws, u["href"], wait_seconds=6.0)
        except Exception as e:
            print(f"  ✗ {u['label'][:40]:40s} (nav failed: {e})")
            continue

        await asyncio.sleep(settle)  # let modal render
        if await _is_cf_challenge(ws):
            print(f"  ⚠ Cloudflare fired mid-walk on {u['label'][:30]} — halting")
            break
        modal = await probe_modal_inputs(ws)

        state = {
            "name": u["label"] or modal.get("modal_title") or u["href"].split("/")[-1],
            "url": u["href"],
            "inputs": modal.get("inputs") or [],
            "buttons": modal.get("buttons") or [],
        }
        if state["inputs"] or modal.get("modal_found"):
            states.append(state)
            print(f"  ✓ {state['name'][:50]:50s} · {len(state['inputs'])} inputs · {len(state['buttons'])} buttons")
        else:
            print(f"  · {state['name'][:50]:50s} (no inputs detected)")
        await asyncio.sleep(inter_step)

    # Return to base URL so we don't leave a modal hanging
    try:
        await _navigate(ws, base_url, wait_seconds=5.0)
    except Exception:
        pass
    return states


async def walk_derived_modals(ws: str, base_url: str) -> list[dict]:
    """Vue/Nuxt route-based modals: derive candidate URLs from edit-trigger labels.
    Upwork pattern: profile_base/modal-<slug>. We slug the labels and try each.
    """
    triggers = await probe_edit_triggers(ws)
    if not triggers:
        return []

    # Build candidate URL list. The known-working pattern from the user's stuck modal:
    #   /freelancers/~XXX/modal-profile-description?pageTitle=Profile%20overview
    base = base_url.rstrip("/")
    # Known Upwork section slugs (derived from common labels + the one we saw work)
    candidates = []
    seen_slugs = set()
    label_map = {
        "edit photo": ("modal-profile-photo", "Photo"),
        "edit availability badge": ("modal-availability-badge", "Availability badge"),
        "edit availability": ("modal-availability", "Availability"),
        "edit boost your profile": ("modal-boost", "Boost"),
        "edit title": ("modal-profile-title", "Title"),
        "edit hourly rate": ("modal-hourly-rate", "Hourly rate"),
        "edit description": ("modal-profile-description", "Profile overview"),
        "edit skills": ("modal-skills", "Skills"),
        "edit language": ("modal-languages", "Languages"),
    }
    for t in triggers:
        key = t["label"].lower().strip()
        # Direct map
        if key in label_map:
            slug, title = label_map[key]
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                candidates.append((slug, title))
        # Education / experience items: slug as modal-employment-history / modal-education-history
        if "education" in key and "modal-education-history" not in seen_slugs:
            seen_slugs.add("modal-education-history")
            candidates.append(("modal-education-history", "Education"))
        if "experience" in key and "modal-employment-history" not in seen_slugs:
            seen_slugs.add("modal-employment-history")
            candidates.append(("modal-employment-history", "Employment"))

    print(f"  derived {len(candidates)} candidate modal URL(s)")
    states: list[dict] = []
    for slug, title in candidates:
        if await _is_cf_challenge(ws):
            print("  ⚠ Cloudflare — halting")
            break
        url = f"{base}/{slug}?pageTitle={urllib.parse.quote(title)}"
        try:
            await _navigate(ws, url, wait_seconds=6.0)
        except Exception as e:
            print(f"  ✗ {slug:35s} (nav failed: {e})")
            continue
        await asyncio.sleep(2.0)

        cur_url = await _eval(ws, "location.href")
        # If Upwork redirected us back to the base (slug doesn't exist), skip.
        if not cur_url or "/modal-" not in cur_url:
            print(f"  · {slug:35s} (no modal — redirected)")
            await asyncio.sleep(1.2)
            continue

        modal = await probe_modal_inputs(ws)
        if modal.get("modal_found") and modal.get("inputs"):
            state = {
                "name": modal.get("modal_title") or title,
                "url": cur_url,
                "slug": slug,
                "inputs": modal.get("inputs") or [],
                "buttons": modal.get("buttons") or [],
            }
            states.append(state)
            print(f"  ✓ {slug:35s} · {len(state['inputs']):2d} inputs · {len(state['buttons']):2d} buttons")
        else:
            print(f"  · {slug:35s} (modal not detected)")

        # Close via history.back to return to base
        await _eval(ws, "window.history.back()")
        await asyncio.sleep(2.0)

    # Make sure we end on base
    cur = await _eval(ws, "location.href")
    if cur and "/modal-" in cur:
        await _navigate(ws, base_url, wait_seconds=5.0)
    return states


async def walk_edit_modals(ws: str) -> list[dict]:
    """Click every edit trigger, capture URL change, probe modal, history.back() to close."""
    triggers = await probe_edit_triggers(ws)
    print(f"\n[explore] found {len(triggers)} edit trigger(s)")
    base_url = await _eval(ws, "location.href")
    states: list[dict] = []
    seen_urls: set[str] = set()
    for t in triggers:
        sel = t["selector"]
        label = t["label"]

        if await _is_cf_challenge(ws):
            print("  ⚠ Cloudflare detected — halting")
            break

        # Get element coordinates, then click via CDP Input.dispatchMouseEvent (trusted events).
        coords_js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return null;
            el.scrollIntoView({{block:'center'}});
            const r = el.getBoundingClientRect();
            return JSON.stringify({{x: r.left + r.width/2, y: r.top + r.height/2}});
        }})()"""
        coords_raw = await _eval(ws, coords_js)
        if coords_raw:
            try:
                pt = json.loads(coords_raw)
                # CDP-trusted click. Vue handlers that ignore JS-dispatched events respect this.
                for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await server.cdp_send(ws, "Input.dispatchMouseEvent", {
                        "type": ev, "x": pt["x"], "y": pt["y"], "button": "left",
                        "clickCount": 1 if ev != "mouseMoved" else 0,
                    })
                    await asyncio.sleep(0.05)
            except Exception as e:
                print(f"      cdp click failed: {e}")
        await asyncio.sleep(2.4)

        cur_url = await _eval(ws, "location.href")
        modal = await probe_modal_inputs(ws)

        if cur_url and cur_url != base_url and cur_url not in seen_urls and modal.get("modal_found"):
            seen_urls.add(cur_url)
            state = {
                "name": modal.get("modal_title") or label,
                "url": cur_url,
                "trigger_label": label,
                "inputs": modal.get("inputs") or [],
                "buttons": modal.get("buttons") or [],
            }
            states.append(state)
            print(f"  ✓ {state['name'][:50]:50s} · {len(state['inputs']):2d} inputs · {len(state['buttons']):2d} buttons")
        else:
            print(f"  · {label[:50]:50s} (no new state)")

        # Close via history.back() — Vue/Nuxt route-bound modals respond to this
        await _eval(ws, "window.history.back()")
        await asyncio.sleep(1.8)
        # Verify we're back at base. If not, navigate explicitly.
        now_url = await _eval(ws, "location.href")
        if now_url != base_url and "modal" in (now_url or "").lower():
            await _navigate(ws, base_url, wait_seconds=5.0)
            await asyncio.sleep(1.5)

    await _eval(ws, r"""document.querySelectorAll('[data-weaver-trigger]').forEach(el => el.removeAttribute('data-weaver-trigger'))""")
    return states


async def probe_pacing(ws: str) -> dict:
    """Detect anti-bot signals → emit a pacing_profile.
       fast    — no signals · ~50ms between steps
       medium  — minor signals (one or two) · 200-500ms
       cautious — Cloudflare/hcaptcha/Akamai present · 1-2s + jitter
    """
    js = r"""(function(){
        const sigs = {
            cloudflare: !!document.querySelector('iframe[src*="cloudflare"], #challenge-form, [data-translate*="cf"]'),
            hcaptcha:   !!document.querySelector('iframe[src*="hcaptcha"], .h-captcha'),
            recaptcha:  !!document.querySelector('iframe[src*="recaptcha"], .g-recaptcha'),
            akamai:     !!(window.bmak || document.cookie.includes('_abck')),
            datadome:   !!(window.DD_RUM || document.cookie.includes('datadome')),
            kasada:     !!document.querySelector('script[src*="kasada"]'),
            perimeter:  !!document.querySelector('script[src*="perimeterx"]')
        };
        const count = Object.values(sigs).filter(Boolean).length;
        let profile;
        if (count === 0) profile = 'fast';
        else if (count <= 1) profile = 'medium';
        else profile = 'cautious';
        return JSON.stringify({signals: sigs, profile: profile});
    })()"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "{}")
    except Exception:
        return {"profile": "medium", "signals": {}}


async def probe_fingerprints(ws: str) -> dict:
    """Durable signals that survive minor DOM tweaks."""
    js = r"""JSON.stringify({
        body_class: (document.body.className || '').toString().slice(0, 300),
        title: document.title || '',
        url: location.href,
        a_declarative_count: document.querySelectorAll('.a-declarative').length,
        a_button_count: document.querySelectorAll('.a-button').length,
        forms_count: document.forms.length,
        next_data: !!window.__NEXT_DATA__,
        nuxt: !!window.__NUXT__,
        react: !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__,
        redux: !!window.__REDUX_DEVTOOLS_EXTENSION__
    })"""
    val = await _eval(ws, js)
    try:
        return json.loads(val or "{}")
    except Exception:
        return {}


# ─── Freeze probes into reusable pre-flight checks ────────────────────────────
def freeze_checks(framework: dict, inputs: list[dict], buttons: list[dict], fp: dict) -> list[dict]:
    """Turn live probe results into a list of frozen check specs.
    Each spec is what `check` mode re-runs to validate the Thread is still green.
    """
    checks: list[dict] = []

    for fw in framework.get("frameworks", []):
        checks.append({"name": f"framework_{fw}", "kind": "framework_marker",
                       "probe": fw, "expected": f"{fw} detected"})

    if fp.get("a_declarative_count", 0) > 20:
        checks.append({"name": "aui_declarative_count", "kind": "js_check",
                       "probe": "document.querySelectorAll('.a-declarative').length > 20",
                       "expected": ".a-declarative elements present (AUI marker)"})
    if "a-aui_" in (fp.get("body_class") or ""):
        checks.append({"name": "aui_body_class", "kind": "js_check",
                       "probe": "/a-aui_/.test(document.body.className)",
                       "expected": "body has a-aui_* class"})

    # Stable inputs — must have id or name
    for el in inputs:
        if el.get("id"):
            checks.append({"name": f"input_{el['id']}", "kind": "selector_exists",
                           "probe": f"#{el['id']}", "expected": f"input #{el['id']}"})
        elif el.get("name"):
            checks.append({"name": f"input_named_{el['name'][:30]}", "kind": "selector_exists",
                           "probe": f"[name=\"{el['name']}\"]", "expected": f"input name={el['name']}"})

    # Buttons — match by text since IDs less common
    seen_text = set()
    for b in buttons:
        t = (b.get("text") or "").lower().strip()
        if t and len(t) > 2 and t not in seen_text and len(t) < 40:
            seen_text.add(t)
            checks.append({"name": f"button_{re.sub(r'[^a-z0-9]+', '_', t)[:30]}",
                           "kind": "button_text", "probe": t, "expected": f"button '{t}'"})

    # Dedup, cap
    seen = set()
    out = []
    for c in checks:
        k = (c["kind"], c["probe"])
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out[:40]


# ─── Run frozen checks (the pre-flight) ──────────────────────────────────────
async def _run_check(ws: str, c: dict) -> tuple[bool, str]:
    try:
        if c["kind"] == "selector_exists":
            r = await _eval(ws, f"!!document.querySelector({json.dumps(c['probe'])})")
            return bool(r), ""
        if c["kind"] == "js_check":
            r = await _eval(ws, f"!!({c['probe']})")
            return bool(r), ""
        if c["kind"] == "page_text":
            r = await _eval(ws, f"(document.body.innerText||'').toLowerCase().includes({json.dumps(c['probe'].lower())})")
            return bool(r), ""
        if c["kind"] == "button_text":
            js = f"""(function(){{
                const t = {json.dumps(c['probe'].lower())};
                for (const b of document.querySelectorAll('button,[role=button],a,input[type=submit]')) {{
                    if (b.offsetParent === null) continue;
                    const x = ((b.textContent || b.value || b.getAttribute('aria-label') || '').trim().toLowerCase());
                    if (x === t || x.includes(t)) return true;
                }}
                return false;
            }})()"""
            r = await _eval(ws, js)
            return bool(r), ""
        if c["kind"] == "framework_marker":
            r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
            data = json.loads(r.get("result", {}).get("value", "{}"))
            fws = data.get("frameworks", [])
            return any(c["probe"] in f for f in fws), f"detected={fws}"
    except Exception as e:
        return False, f"ERROR: {e}"
    return False, "unknown kind"


async def run_checks(ws: str, checks: list[dict]) -> tuple[int, int, list[str]]:
    passed = failed = 0
    failures = []
    for c in checks:
        ok, detail = await _run_check(ws, c)
        status = "✓" if ok else "✗"
        print(f"  {status} {c['name']:40s} — {c['expected']}{(' · ' + detail) if detail and not ok else ''}")
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append(f"{c['name']}: {c['expected']}")
    return passed, failed, failures


# ─── Modes ───────────────────────────────────────────────────────────────────
async def cmd_discover(url: str, port: int, explore: bool = False):
    domain = urllib.parse.urlparse(url).netloc
    print(f"\n[weaver discover] {url}")
    print(f"  domain: {domain}")

    priors = gather_priors(domain)

    print(f"\n[probe] connecting to Chrome on port {port}")
    ws = await _pick_tab(port, None)
    await _navigate(ws, url)
    print(f"  loaded: {url}")

    print("\n[probe] anti-bot")
    ab_r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    try:
        ab = json.loads(ab_r.get("result", {}).get("value", "{}"))
        print(f"  verdict: {ab.get('verdict')}")
    except Exception:
        ab = {"verdict": "unknown"}

    print("\n[probe] framework, inputs, buttons, fingerprints, pacing...")
    framework = await probe_framework(ws)
    inputs = await probe_inputs(ws)
    buttons = await probe_buttons(ws)
    fp = await probe_fingerprints(ws)
    pacing = await probe_pacing(ws)
    print(f"  frameworks: {framework.get('frameworks')}")
    print(f"  inputs: {len(inputs)} · buttons: {len(buttons)}")
    fs_summary = {}
    for el in inputs:
        s = el.get("fill_strategy", "?")
        fs_summary[s] = fs_summary.get(s, 0) + 1
    cs_summary = {}
    for b in buttons:
        s = b.get("click_strategy", "?")
        cs_summary[s] = cs_summary.get(s, 0) + 1
    print(f"  fill strategies:  {fs_summary}")
    print(f"  click strategies: {cs_summary}")
    print(f"  pacing profile:   {pacing.get('profile')} · signals: {[k for k,v in pacing.get('signals',{}).items() if v]}")

    checks = freeze_checks(framework, inputs, buttons, fp)
    print(f"\n[freeze] {len(checks)} pre-flight checks generated")

    states: list[dict] = []
    if explore:
        # Strategy 1: URL-routable links (rare).
        states = await walk_url_modals(ws, url)
        # Strategy 2: Vue/Nuxt route-based modals — derive URL candidates from edit-trigger labels.
        if not states:
            print("  no <a href> modals — deriving modal URLs from edit-trigger labels...")
            states = await walk_derived_modals(ws, url)
        # Strategy 3: last resort — click + history.back() walking.
        if not states:
            print("  derived URLs found nothing — falling back to click walk")
            states = await walk_edit_modals(ws)
        total_modal_inputs = sum(len(s.get("inputs", [])) for s in states)
        print(f"\n[explore] captured {len(states)} modal state(s) · {total_modal_inputs} additional inputs")

    thread = {
        "domain": domain,
        "name": f"{domain} Thread",
        "version": "1.0.0",
        "author": "weaver-auto",
        "license": "open",
        "created_at": int(time.time()),
        "seed_url": url,
        "anti_bot": ab,
        "framework": framework,
        "fingerprints": fp,
        "pacing": pacing,
        "inputs": inputs,
        "buttons": buttons,
        "priors": priors,
        "preflight": checks,
        "states": states,
    }
    out = THREADS_DIR / f"{domain}.thread.json"
    if out.exists():
        bak = out.with_suffix(f".bak.{int(time.time())}.json")
        bak.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  backed up existing thread → {bak.name}")
    out.write_text(json.dumps(thread, indent=2), encoding="utf-8")
    print(f"\n  ✅ Thread written: {out}")
    print(f"  Re-validate anytime with:  python weaver.py check {domain} --port {port}")


async def cmd_check(domain: str, port: int):
    path = THREADS_DIR / f"{domain}.thread.json"
    if not path.exists():
        print(f"  ✗ no Thread found at {path}")
        print(f"    Build one with: python weaver.py discover https://{domain}/ --port {port}")
        return 1
    thread = json.loads(path.read_text(encoding="utf-8"))
    checks = thread.get("preflight") or []
    if not checks:
        print(f"  ✗ Thread has no frozen pre-flight (legacy format). Re-run discover.")
        return 1

    print(f"\n[weaver check] {domain} · {len(checks)} frozen probes")
    ws = await _pick_tab(port, domain)
    seed = thread.get("seed_url") or f"https://{domain}/"
    await _navigate(ws, seed)
    print(f"  loaded: {seed}\n")

    passed, failed, failures = await run_checks(ws, checks)
    total = passed + failed
    print("\n" + "=" * 60)
    if failed == 0:
        print(f"  ✅ ALL GREEN — {passed}/{total} checks passed")
        verdict = "green"
    else:
        print(f"  ⚠️  DRIFT — {passed}/{total} passed, {failed} failed")
        for f_ in failures:
            print(f"    • {f_}")
        print(f"\n  Repair: python weaver.py discover {seed} --port {port}")
        verdict = "red"
    print("=" * 60)

    status_path = Path.home() / ".webloom" / "logs" / f"{domain}-check.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({"verdict": verdict, "passed": passed,
                                       "failed": failed, "failures": failures,
                                       "ts": int(time.time())}, indent=2), encoding="utf-8")
    return 0 if failed == 0 else 1


# ─── Apply mode (recipe runner with safety rails) ────────────────────────────
async def _snapshot_hash(ws: str) -> str:
    """Hash of visible-region DOM signature. Used to detect 'did anything change?'"""
    js = r"""(function(){
        const s = (document.body.innerText || '').slice(0, 4000);
        let h = 0; for (let i=0;i<s.length;i++){ h = (h*31 + s.charCodeAt(i)) | 0; }
        return String(h) + ':' + document.querySelectorAll('*').length;
    })()"""
    return str(await _eval(ws, js))


async def _step_fill(ws: str, sel: str, value: str, strategy: str = "fast_setter") -> tuple[bool, str]:
    # Strategy-aware single-shot fill. Order: lexical → contenteditable → react/fast setter.
    if strategy == "lexical_set":
        js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return 'no_element';
            const ed = el.__lexicalEditor || el.closest('[data-lexical-editor]')?.__lexicalEditor;
            if (!ed) return 'no_lexical';
            ed.update(() => {{
                const root = ed.getRootElement ? ed.getEditorState().read(()=>null) : null;
                ed.dispatchCommand && ed.dispatchCommand((window.SELECT_ALL_COMMAND || 'selectAll'));
            }});
            el.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, {json.dumps(value)});
            return 'ok';
        }})()"""
    elif strategy == "contenteditable":
        js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return 'no_element';
            el.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, {json.dumps(value)});
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            return 'ok';
        }})()"""
    elif strategy == "select_option":
        js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return 'no_element';
            const v = {json.dumps(value)};
            for (const o of el.options) {{
                if (o.value === v || o.textContent.trim() === v) {{ el.value = o.value; break; }}
            }}
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return el.value === {json.dumps(value)} || Array.from(el.options).some(o => o.value === el.value) ? 'ok' : 'no_match';
        }})()"""
    else:
        # react_setter / fast_setter — same fast path
        js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return 'no_element';
            const proto = Object.getPrototypeOf(el);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, {json.dumps(value)});
            else el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return el.value === {json.dumps(value)} ? 'ok' : 'value_mismatch';
        }})()"""
    r = await _eval(ws, js)
    return (r == "ok"), f"[{strategy}] {r or ''}"


async def _step_click_selector(ws: str, sel: str, strategy: str = "js_dispatch") -> tuple[bool, str]:
    if strategy == "aui_fire":
        js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return 'no_element';
            const host = el.closest('.a-declarative[data-action]');
            if (host && window.A?.declarative?.fire) {{
                try {{ window.A.declarative.fire(host.getAttribute('data-action'), host, new Event('click')); return 'ok'; }} catch(e) {{ return 'aui_err:' + e.message; }}
            }}
            return 'no_aui';
        }})()"""
    else:
        # js_dispatch / radix_portal / react_handler all share the leaf event sequence
        js = f"""(function(){{
            const el = document.querySelector({json.dumps(sel)});
            if (!el) return 'no_element';
            if (el.offsetParent === null && el.tagName !== 'OPTION') return 'not_visible';
            el.scrollIntoView({{block:'center', behavior:'instant'}});
            const r = el.getBoundingClientRect();
            const x = r.left + r.width/2, y = r.top + r.height/2;
            for (const t of ['pointerdown','mousedown','pointerup','mouseup','click']) {{
                el.dispatchEvent(new MouseEvent(t, {{bubbles:true, cancelable:true, view:window, clientX:x, clientY:y, button:0}}));
            }}
            return 'ok';
        }})()"""
    r = await _eval(ws, js)
    return (r == "ok"), f"[{strategy}] {r or ''}"


async def _step_click_text(ws: str, text: str) -> tuple[bool, str]:
    js = f"""(function(){{
        const t = {json.dumps(text.lower())};
        for (const b of document.querySelectorAll('button,[role=button],a,input[type=submit]')) {{
            if (b.offsetParent === null) continue;
            const x = ((b.textContent||b.value||b.getAttribute('aria-label')||'').trim().toLowerCase());
            if (x === t || x.includes(t)) {{
                const r = b.getBoundingClientRect();
                const cx = r.left+r.width/2, cy = r.top+r.height/2;
                for (const e of ['pointerdown','mousedown','pointerup','mouseup','click']) {{
                    b.dispatchEvent(new MouseEvent(e, {{bubbles:true,cancelable:true,view:window,clientX:cx,clientY:cy,button:0}}));
                }}
                return 'ok';
            }}
        }}
        return 'not_found';
    }})()"""
    r = await _eval(ws, js)
    return (r == "ok"), r or ""


async def _step_wait_selector(ws: str, sel: str, timeout: float = 8.0) -> tuple[bool, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await _eval(ws, f"!!document.querySelector({json.dumps(sel)})")
        if r:
            return True, "found"
        await asyncio.sleep(0.25)
    return False, "timeout"


async def _step_wait_text(ws: str, text: str, timeout: float = 8.0) -> tuple[bool, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await _eval(ws, f"(document.body.innerText||'').toLowerCase().includes({json.dumps(text.lower())})")
        if r:
            return True, "found"
        await asyncio.sleep(0.25)
    return False, "timeout"


async def _step_assert(ws: str, kind: str, probe: str) -> tuple[bool, str]:
    ok, detail = await _run_check(ws, {"kind": kind, "probe": probe, "name": "assert", "expected": probe})
    return ok, detail


def _lookup_fill_strategy(thread: dict, selector: str) -> str:
    """Match a recipe selector against Thread inputs to recover the learned fill_strategy."""
    sel = selector.strip()
    sel_id = sel[1:] if sel.startswith("#") else None
    for el in thread.get("inputs", []):
        if sel_id and el.get("id") == sel_id:
            return el.get("fill_strategy") or "fast_setter"
        if el.get("name") and f"[name=\"{el['name']}\"]" in sel:
            return el.get("fill_strategy") or "fast_setter"
        if el.get("id") and f"#{el['id']}" in sel:
            return el.get("fill_strategy") or "fast_setter"
    return "fast_setter"


def _lookup_click_strategy(thread: dict, selector: str) -> str:
    sel_id = selector[1:] if selector.strip().startswith("#") else None
    for b in thread.get("buttons", []):
        if sel_id and b.get("id") == sel_id:
            return b.get("click_strategy") or "js_dispatch"
        if b.get("id") and f"#{b['id']}" in selector:
            return b.get("click_strategy") or "js_dispatch"
    return "js_dispatch"


def _pacing_default_pause(thread: dict) -> float:
    profile = (thread.get("pacing") or {}).get("profile", "medium")
    return {"fast": 0.08, "medium": 0.4, "cautious": 1.4}.get(profile, 0.4)


async def run_recipe(ws: str, steps: list[dict], dry_run: bool, thread: dict | None = None, halt_on_failure: bool = True) -> int:
    thread = thread or {}
    default_pause = _pacing_default_pause(thread)
    profile = (thread.get("pacing") or {}).get("profile", "medium")
    print(f"\n[apply] {len(steps)} steps · dry_run={dry_run} · pacing={profile} (default pause {default_pause:.2f}s)\n")
    for i, step in enumerate(steps, 1):
        kind = step.get("kind")
        label = step.get("label") or kind
        print(f"  [{i:02d}/{len(steps):02d}] {kind:18s} · {label}")
        if dry_run:
            print(f"         (dry-run) skipping execute")
            continue

        before = await _snapshot_hash(ws)
        ok, detail = False, ""
        try:
            if kind == "fill":
                strat = step.get("strategy") or _lookup_fill_strategy(thread, step["selector"])
                ok, detail = await _step_fill(ws, step["selector"], step["value"], strat)
            elif kind == "click_selector":
                strat = step.get("strategy") or _lookup_click_strategy(thread, step["selector"])
                ok, detail = await _step_click_selector(ws, step["selector"], strat)
            elif kind == "click_text":
                ok, detail = await _step_click_text(ws, step["text"])
            elif kind == "wait_selector":
                ok, detail = await _step_wait_selector(ws, step["selector"], step.get("timeout", 8))
            elif kind == "wait_text":
                ok, detail = await _step_wait_text(ws, step["text"], step.get("timeout", 8))
            elif kind == "assert_selector":
                ok, detail = await _step_assert(ws, "selector_exists", step["selector"])
            elif kind == "assert_text":
                ok, detail = await _step_assert(ws, "page_text", step["text"])
            elif kind == "sleep":
                await asyncio.sleep(float(step.get("seconds", 1)))
                ok, detail = True, "slept"
            elif kind == "screenshot":
                shot_path = Path.home() / ".webloom" / "logs" / f"apply-{int(time.time())}-{i}.png"
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    img_b64 = await server.screenshot_tab(ws)
                    import base64
                    shot_path.write_bytes(base64.b64decode(img_b64))
                    ok, detail = True, str(shot_path.name)
                except Exception as e:
                    ok, detail = False, f"screenshot failed: {e}"
            else:
                ok, detail = False, f"unknown kind: {kind}"
        except Exception as e:
            ok, detail = False, f"ERROR: {e}"

        after = await _snapshot_hash(ws)
        changed = "Δ" if before != after else " "
        status = "✓" if ok else "✗"
        print(f"         {status} {detail}  {changed}")

        if not ok and halt_on_failure:
            print(f"\n  ⚠ HALTED at step {i}. No further steps executed.")
            return 1

        # Pacing-aware pause; step can override
        await asyncio.sleep(float(step.get("pause", default_pause)))

    print(f"\n  ✅ recipe complete")
    return 0


async def cmd_apply(domain: str, recipe_path: str, port: int, dry_run: bool, skip_check: bool):
    thread_path = THREADS_DIR / f"{domain}.thread.json"
    if not thread_path.exists():
        print(f"  ✗ no Thread for {domain}. Run `weaver discover` first.")
        return 1
    rpath = Path(recipe_path)
    if not rpath.exists():
        print(f"  ✗ recipe not found: {rpath}")
        return 1
    recipe = json.loads(rpath.read_text(encoding="utf-8"))
    steps = recipe.get("steps") or []
    if not steps:
        print(f"  ✗ recipe has no steps")
        return 1

    print(f"\n[weaver apply] {domain} · recipe: {rpath.name}")
    print(f"  Thread: {thread_path.name}")
    print(f"  Steps:  {len(steps)}")
    print(f"  Mode:   {'DRY-RUN (no changes)' if dry_run else 'LIVE (will modify site state)'}")

    ws = await _pick_tab(port, domain)

    if not skip_check:
        thread = json.loads(thread_path.read_text(encoding="utf-8"))
        checks = thread.get("preflight") or []
        if checks:
            print(f"\n[pre-flight] {len(checks)} probes against current page")
            p, f, _ = await run_checks(ws, checks)
            if f > 0:
                print(f"\n  ⚠ pre-flight failed ({f} drift). Refusing to run recipe.")
                print(f"    Repair with: python weaver.py discover <url> --port {port}")
                print(f"    Or override: --skip-check (NOT RECOMMENDED)")
                return 1

    thread = json.loads(thread_path.read_text(encoding="utf-8"))
    return await run_recipe(ws, steps, dry_run=dry_run, thread=thread)


def cmd_promote(domain: str, min_successes: int = 1) -> int:
    """Read playbook entries for this domain → merge into Thread.

    Uses the enriched action_log when present (v2 — captures sequence, wait timings,
    manual-touch markers, selector patterns), falls back to legacy click_log for
    older playbook data.
    """
    pb_path = Path(os.environ.get("WEBLOOM_PLAYBOOK", "")) if os.environ.get("WEBLOOM_PLAYBOOK") else None
    if not pb_path or not pb_path.exists():
        for candidate in [Path("D:/BrowserSessions/playbook.json"),
                          Path.home() / ".webloom" / "playbook.json"]:
            if candidate.exists():
                pb_path = candidate
                break
    if not pb_path or not pb_path.exists():
        print("  ✗ no playbook found")
        return 1

    pb = json.loads(pb_path.read_text(encoding="utf-8"))
    domain_data = pb.get(domain) or {}
    action_log = domain_data.get("action_log") or {}
    click_log = domain_data.get("click_log") or {}
    # Coverage gaps: bugs to crack, not features. Open gaps surface publicly so
    # the marketplace can prioritize fixes. Legacy "manual_touches" key still
    # read for backward-compat with playbooks recorded before the rename.
    coverage_gaps = domain_data.get("coverage_gaps") or domain_data.get("manual_touches") or []

    if not action_log and not click_log:
        print(f"  ✗ no playbook entries for {domain}")
        print(f"    Drive the site via WebLoom (clicks/fills) to populate the playbook.")
        return 1

    thread_path = THREADS_DIR / f"{domain}.thread.json"
    if not thread_path.exists():
        print(f"  ⚠ no Thread for {domain} — run discover first")
        return 1
    thread = json.loads(thread_path.read_text(encoding="utf-8"))

    # ── Build enriched proven_actions (prefers action_log) ────────────────
    proven = []
    seen_descs = set()
    for desc, info in action_log.items():
        if info.get("successes", 0) < min_successes or info.get("failures", 0) > 0:
            continue
        seen_descs.add(desc)
        wb = info.get("wait_before_samples") or []
        wa = info.get("wait_after_samples") or []
        avg_wb = round(sum(wb) / len(wb), 2) if wb else None
        avg_wa = round(sum(wa) / len(wa), 2) if wa else None
        proven.append({
            "descriptor": desc,
            "kind": info.get("kind", "click"),
            "strategy": info.get("strategy"),
            "successes": info.get("successes", 0),
            "last_at": info.get("last_at"),
            "selector_pattern": info.get("selector_pattern"),
            "follows": info.get("follows") or [],
            "wait_before_s": avg_wb,
            "wait_after_s": avg_wa,
            "manual_touch_required": info.get("manual_touch_required", False),
            "manual_touch_reason": info.get("manual_touch_reason"),
        })
    # Backfill from legacy click_log for any descriptor not in action_log
    for desc, info in click_log.items():
        if desc in seen_descs:
            continue
        if (info.get("successes", 0) >= min_successes) and info.get("failures", 0) == 0:
            proven.append({
                "descriptor": desc,
                "kind": "click",
                "strategy": info.get("strategy"),
                "successes": info.get("successes", 0),
                "last_at": info.get("last_at"),
            })

    # Sort: actions with no predecessors come first (likely flow starts), then by successes
    proven.sort(key=lambda x: (len(x.get("follows") or []), -x.get("successes", 0)))

    # ── Reconstruct recipe sequence from `follows` graph ──────────────────
    # Build a partial order: action A → action B if B.follows contains A.
    # Simple topological-ish sort; not perfect but useful for buyers to see flow order.
    recipe_steps = _topo_sort_actions(proven)

    thread["proven_actions"] = proven
    if recipe_steps:
        thread["recipe_steps"] = recipe_steps
    # Coverage gaps: only OPEN ones surface in the Thread. Fixed gaps stay in the
    # playbook history but don't pollute the public Thread file.
    open_gaps = [g for g in coverage_gaps if g.get("status", "open") == "open"]
    if open_gaps:
        thread["coverage_gaps"] = open_gaps
    else:
        thread.pop("coverage_gaps", None)
        thread.pop("manual_touches", None)  # clean legacy field if present

    bak = thread_path.with_suffix(f".bak.{int(time.time())}.json")
    bak.write_text(thread_path.read_text(encoding="utf-8"), encoding="utf-8")
    thread_path.write_text(json.dumps(thread, indent=2), encoding="utf-8")

    coverage_pct = 100 if not open_gaps else max(0, 100 - len(open_gaps) * 10)
    print(f"\n  ✅ promoted {len(proven)} action(s) into {thread_path.name}")
    print(f"     {len(recipe_steps)} ordered steps · coverage ≈ {coverage_pct}% · {len(open_gaps)} open gap(s)")
    for p in proven[:15]:
        gap_flag = " 🔧 GAP" if p.get("manual_touch_required") else ""
        kind = p.get("kind", "click")
        wb = f" (~{p['wait_before_s']}s after)" if p.get("wait_before_s") else ""
        print(f"    [{p.get('strategy', '?'):8s}] {kind:10s} x{p['successes']}{wb}  {p['descriptor'][:50]}{gap_flag}")
    if len(proven) > 15:
        print(f"    ... + {len(proven) - 15} more")
    if open_gaps:
        print(f"\n  🔧 Open gaps (TODO crack these):")
        for g in open_gaps[:5]:
            print(f"    - {g.get('desc', '?'):40s}  [{g.get('classification', 'unknown')}]")
        if len(open_gaps) > 5:
            print(f"    ... + {len(open_gaps) - 5} more")
    print(f"\n  backup: {bak.name}")
    return 0


def _topo_sort_actions(proven: list[dict]) -> list[dict]:
    """Best-effort topological sort using `follows` edges. Returns ordered steps."""
    by_desc = {p["descriptor"]: p for p in proven}
    visited: set[str] = set()
    ordered: list[dict] = []

    def visit(d: str, stack: set[str]):
        if d in visited or d in stack:
            return
        stack.add(d)
        p = by_desc.get(d)
        if p:
            for prev in p.get("follows") or []:
                if prev in by_desc:
                    visit(prev, stack)
            visited.add(d)
            ordered.append({
                "i": len(ordered),
                "kind": p.get("kind"),
                "descriptor": p["descriptor"],
                "strategy": p.get("strategy"),
                "selector_pattern": p.get("selector_pattern"),
                "wait_after_s": p.get("wait_after_s"),
                "manual_touch_required": p.get("manual_touch_required", False),
            })
        stack.discard(d)

    for d in by_desc.keys():
        visit(d, set())
    return ordered


def cmd_list():
    threads = sorted(THREADS_DIR.glob("*.thread.json"))
    print(f"\n{len(threads)} Threads at {THREADS_DIR}\n")
    for p in threads:
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            checks = len(t.get("preflight") or [])
            ts = t.get("created_at", 0)
            age_days = (time.time() - ts) / 86400 if ts else 0
            print(f"  {t.get('domain', p.stem):40s} · {checks:3d} checks · {age_days:.0f}d old")
        except Exception:
            print(f"  {p.stem:40s} · (unreadable)")


def main():
    p = argparse.ArgumentParser(description="WebLoom Weaver — Thread builder + validator")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="Build a new Thread from a live site")
    d.add_argument("url")
    d.add_argument("--port", type=int, default=9226)
    d.add_argument("--explore", action="store_true", help="Walk every edit-trigger and probe each modal as a state")

    c = sub.add_parser("check", help="Re-validate an existing Thread")
    c.add_argument("domain")
    c.add_argument("--port", type=int, default=9226)

    a = sub.add_parser("apply", help="Run a recipe against a Thread (pre-flight first, halt on drift)")
    a.add_argument("domain")
    a.add_argument("recipe", help="Path to recipe JSON")
    a.add_argument("--port", type=int, default=9226)
    a.add_argument("--dry-run", action="store_true", help="Preview steps without executing")
    a.add_argument("--skip-check", action="store_true", help="Skip pre-flight (NOT RECOMMENDED)")

    pr = sub.add_parser("promote", help="Read playbook for domain → merge proven strategies into Thread")
    pr.add_argument("domain")
    pr.add_argument("--min-successes", type=int, default=1)

    sub.add_parser("list", help="List installed Threads")

    args = p.parse_args()
    if args.cmd == "discover":
        sys.exit(asyncio.run(cmd_discover(args.url, args.port, args.explore)) or 0)
    elif args.cmd == "check":
        sys.exit(asyncio.run(cmd_check(args.domain, args.port)) or 0)
    elif args.cmd == "apply":
        sys.exit(asyncio.run(cmd_apply(args.domain, args.recipe, args.port, args.dry_run, args.skip_check)) or 0)
    elif args.cmd == "promote":
        sys.exit(cmd_promote(args.domain, args.min_successes) or 0)
    elif args.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
