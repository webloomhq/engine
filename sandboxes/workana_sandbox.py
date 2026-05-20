"""Workana sandbox.

Latin America's #1 freelance platform. Uruguay residency = explicit profile
advantage. This sandbox probes the public + (if logged in) authenticated UI
to crack profile editing, proposal submission, and dashboard navigation
before any production runs.

Two phases:
  1. ANONYMOUS — works without login. Probes framework, anti-bot, job listing
     structure, public profile pages, signup/login form.
  2. AUTHENTICATED — runs only if a logged-in dashboard URL is reachable.
     Probes profile edit modals, proposal submission flow, message system.

NEVER submits any proposal or saves any profile change.

Usage:
    python workana_sandbox.py --port 9226          # uses slot-2
    python workana_sandbox.py --port 9226 --auto   # use whatever Workana tab is open
"""
import argparse
import asyncio
import io
import json
import sys
import time
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


def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


def _pick_workana_tab(port: int) -> dict | None:
    for t in _list_tabs(port):
        if t.get("type") == "page" and "workana.com" in (t.get("url", "") or ""):
            return t
    return None


async def _navigate(ws: str, url: str, wait_seconds: float = 8.0):
    await server.cdp_send(ws, "Page.navigate", {"url": url})
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(2.0)


# ── probes ─────────────────────────────────────────────────────────────────────
async def probe_framework(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def probe_anti_bot(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


PROBE_LOGIN_STATE_JS = r"""(function() {
    // Heuristics: logged-in users see avatar/profile link + dashboard items;
    // logged-out users see "Login" / "Iniciar sesión" / "Sign up" / "Registrarse"
    const html = (document.body && document.body.innerText || '').toLowerCase();
    const isLoggedIn = (
        !!document.querySelector('[data-test*="user-menu" i], .user-avatar, [class*="user-menu" i], [class*="user-account" i]')
        && !/iniciar sesi.n|sign in|log\s?in/i.test(html.slice(0, 500))
    );
    const loginLink = document.querySelector('a[href*="login" i], a[href*="iniciar" i]');
    const signupLink = document.querySelector('a[href*="register" i], a[href*="signup" i], a[href*="registrate" i]');
    return JSON.stringify({
        isLoggedIn,
        login_link_href: loginLink?.getAttribute('href') || null,
        signup_link_href: signupLink?.getAttribute('href') || null,
        page_lang: document.documentElement.lang || 'unknown',
        title: document.title,
        url: location.href,
    });
})()"""


async def probe_login_state(ws: str) -> dict:
    r = await server.eval_in_tab(ws, PROBE_LOGIN_STATE_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


PROBE_JOB_FEED_JS = r"""(function() {
    // Workana job cards: typically <li class="project-item"> or [data-test*="job"]
    const cards = document.querySelectorAll(
        '.project-item, [data-test*="job" i], [data-test*="project" i], '
        + 'article[class*="project" i], li[class*="project" i]'
    );
    const out = [];
    for (let i = 0; i < Math.min(5, cards.length); i++) {
        const c = cards[i];
        const title = c.querySelector('h2, h3, [class*="title" i] a')?.textContent?.trim().slice(0, 100) || '';
        const link = c.querySelector('a[href*="/messages/job"], a[href*="/jobs/"], a[href*="/projects/"]')?.getAttribute('href') || '';
        const budget = c.querySelector('[class*="budget" i], [data-test*="budget" i]')?.textContent?.trim().slice(0, 60) || '';
        out.push({ title, link, budget });
    }
    return JSON.stringify({
        card_count: cards.length,
        first_5: out,
        list_selectors_found: {
            project_item: !!document.querySelector('.project-item'),
            data_test_job: !!document.querySelector('[data-test*="job" i]'),
        }
    });
})()"""


async def probe_job_feed(ws: str) -> dict:
    r = await server.eval_in_tab(ws, PROBE_JOB_FEED_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


PROBE_LOGIN_FORM_JS = r"""(function() {
    const email = document.querySelector('input[type="email"], input[name*="email" i], input[name="username"]');
    const pwd = document.querySelector('input[type="password"]');
    const submit = document.querySelector('button[type="submit"], input[type="submit"]');
    const oauth = Array.from(document.querySelectorAll('button, a')).filter(el => {
        const t = (el.textContent || '').toLowerCase();
        return /google|facebook|linkedin|apple|github/i.test(t);
    }).map(el => ({text: (el.textContent || '').trim().slice(0, 40), href: el.getAttribute?.('href') || ''}));
    return JSON.stringify({
        has_login_form: !!(email && pwd),
        email_selector: email ? (email.id ? '#'+email.id : email.name ? 'input[name="'+email.name+'"]' : 'input[type=email]') : null,
        password_selector: pwd ? (pwd.id ? '#'+pwd.id : 'input[type=password]') : null,
        submit_present: !!submit,
        oauth_options: oauth.slice(0, 6),
    });
})()"""


async def probe_login_form(ws: str) -> dict:
    r = await server.eval_in_tab(ws, PROBE_LOGIN_FORM_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


# Authenticated probes (only run if logged in)
PROBE_DASHBOARD_JS = r"""(function() {
    // Logged-in dashboard features
    return JSON.stringify({
        url: location.href,
        title: document.title,
        has_proposals_link: !!document.querySelector('a[href*="proposal" i], a[href*="propuesta" i]'),
        has_messages_link: !!document.querySelector('a[href*="messages" i], a[href*="mensaje" i]'),
        has_profile_link: !!document.querySelector('a[href*="/profile/" i], a[href*="/perfil/" i]'),
        active_proposal_count_visible: document.querySelectorAll('[class*="proposal" i][class*="active" i]').length,
    });
})()"""


PROBE_PROFILE_EDIT_JS = r"""(function() {
    // Look for edit-pencil patterns on the profile page
    const editTriggers = Array.from(document.querySelectorAll(
        '[aria-label*="edit" i], [aria-label*="editar" i], button[aria-label*="edit" i], '
        + '[data-test*="edit" i], a[href*="edit" i]'
    )).filter(el => el.offsetParent !== null);
    return JSON.stringify({
        edit_trigger_count: editTriggers.length,
        first_5_triggers: editTriggers.slice(0, 5).map(el => ({
            aria: el.getAttribute('aria-label') || '',
            tag: el.tagName,
            href: el.getAttribute('href') || '',
        }))
    });
})()"""


# ── orchestrator ───────────────────────────────────────────────────────────────
async def run_sandbox(port: int, auto: bool):
    print(f"\n[workana-sandbox] connecting to Chrome on port {port}\n")

    if auto:
        t = _pick_workana_tab(port)
        if t:
            ws = t["webSocketDebuggerUrl"]
            print(f"  using existing tab: {t.get('url','')[:100]}")
        else:
            print("  no workana tab found, will navigate")
            ws = next(t["webSocketDebuggerUrl"] for t in _list_tabs(port) if t.get("type") == "page")
            await _navigate(ws, "https://www.workana.com/")
    else:
        ws = next(t["webSocketDebuggerUrl"] for t in _list_tabs(port) if t.get("type") == "page")
        await _navigate(ws, "https://www.workana.com/")
        print(f"  navigated to: https://www.workana.com/")

    # ── 1. anti-bot
    print("[1] anti-bot probe")
    ab = await probe_anti_bot(ws)
    print(f"    verdict: {ab.get('verdict')}")
    for s in ab.get("signals", []):
        print(f"      - {s.get('type')} ({s.get('confidence', '?')})")

    # ── 2. framework
    print("\n[2] framework detection")
    fw = await probe_framework(ws)
    print(f"    primary: {fw.get('primary')}")
    print(f"    all:     {', '.join(fw.get('frameworks', []))}")

    # ── 3. login state
    print("\n[3] login state")
    login = await probe_login_state(ws)
    is_in = login.get("isLoggedIn")
    print(f"    logged_in: {is_in}")
    print(f"    page_lang: {login.get('page_lang')}")
    print(f"    login_link: {login.get('login_link_href')}")
    print(f"    signup_link: {login.get('signup_link_href')}")

    # ── 4. job feed structure (anonymous works for public listings)
    print("\n[4] job feed probe")
    await _navigate(ws, "https://www.workana.com/jobs?category=it-programming")
    feed = await probe_job_feed(ws)
    print(f"    cards found: {feed.get('card_count')}")
    print(f"    list selectors: {feed.get('list_selectors_found')}")
    for j in feed.get("first_5", []):
        print(f"      - [{j.get('budget', '?')}] {j.get('title','')[:80]}")

    # ── 5. login form probe (navigate to login page anonymously)
    print("\n[5] login form probe")
    await _navigate(ws, "https://www.workana.com/login")
    form = await probe_login_form(ws)
    print(f"    login form present: {form.get('has_login_form')}")
    print(f"    email selector:     {form.get('email_selector')}")
    print(f"    password selector:  {form.get('password_selector')}")
    print(f"    oauth options:      {[o.get('text') for o in form.get('oauth_options', [])]}")

    # ── 6. authenticated phase (only if logged in)
    dash_info = None
    profile_info = None
    if is_in:
        print("\n[6] AUTHENTICATED PROBE — dashboard")
        await _navigate(ws, "https://www.workana.com/dashboard")
        dash_r = await server.eval_in_tab(ws, PROBE_DASHBOARD_JS)
        dash_info = json.loads(dash_r.get("result", {}).get("value", "{}"))
        for k, v in dash_info.items():
            print(f"    {k}: {v}")

        print("\n[7] AUTHENTICATED PROBE — profile edit triggers")
        # Workana profile is usually /freelancers/<id> for the logged-in user
        # We try the "Edit profile" route first
        await _navigate(ws, "https://www.workana.com/freelancer")
        prof_r = await server.eval_in_tab(ws, PROBE_PROFILE_EDIT_JS)
        profile_info = json.loads(prof_r.get("result", {}).get("value", "{}"))
        print(f"    edit triggers found: {profile_info.get('edit_trigger_count')}")
        for t in profile_info.get('first_5_triggers', []):
            print(f"      - aria='{t.get('aria')}' tag={t.get('tag')} href={t.get('href')}")
    else:
        print("\n[6] AUTHENTICATED PROBE — skipped (not logged in)")
        print("    To probe profile + proposal submission flow, log in to Workana on this slot")
        print("    and re-run this sandbox. Mariano's Uruguay residency is an advantage here.")

    # ── 8. write Thread
    print("\n[8] writing workana.com.thread.json")
    notes = [
        f"Workana primary lang: {login.get('page_lang')}. Spanish UI common since LatAm-focused — submit button text often 'Enviar' not 'Submit'.",
        "Job listings on /jobs use .project-item cards or [data-test*='job']. List view + detail view separate.",
        "Login form is at /login. Has standard email+password + OAuth options (Google/Facebook/LinkedIn typically).",
    ]
    if is_in:
        notes.append(f"AUTHENTICATED probe ran. Dashboard URL: {dash_info.get('url') if dash_info else '/dashboard'}. Edit triggers: {profile_info.get('edit_trigger_count') if profile_info else 0}.")
    else:
        notes.append("ANONYMOUS-only probe — log in and re-run for proposal submission + profile edit selectors.")

    thread = {
        "domain": "workana.com",
        "name": "Workana Profile + Proposals (anonymous probe)" if not is_in else "Workana Authenticated Profile",
        "version": "0.1.0",
        "author": "workana_sandbox.py",
        "license": "cc-by",
        "tier": "starter" if not is_in else "starter-authed",
        "framework": fw.get("primary", "vanilla"),
        "frameworks_detected": fw.get("frameworks", []),
        "anti_bot_verdict": ab.get("verdict"),
        "default_strategy": "cdp",
        "login_state": "authenticated" if is_in else "anonymous",
        "page_lang": login.get("page_lang"),
        "notes": notes,
        "selectors": {
            "login_email": form.get("email_selector"),
            "login_password": form.get("password_selector"),
            "login_url": "https://www.workana.com/login",
            "signup_url": "https://www.workana.com/" + (login.get("signup_link_href") or "/register"),
            "job_card": ".project-item, [data-test*='job' i]",
            "edit_trigger_pattern": "[aria-label*='edit' i], [aria-label*='editar' i], [data-test*='edit' i]",
        },
        "quirks": {
            "ui_language": login.get("page_lang") or "es-ES expected",
            "submit_button_text_es": "enviar, guardar, publicar",
            "submit_button_text_en": "submit, save, send",
            "oauth_options": [o.get("text") for o in form.get("oauth_options", [])],
        },
        "actions": {
            "login": {
                "url": "https://www.workana.com/login",
                "email_selector": form.get("email_selector"),
                "password_selector": form.get("password_selector"),
                "credentials_vault_key": "workana",
            }
        },
        "validated_at": int(time.time()),
        "validated_by": "workana_sandbox.py (read-only, no edits)",
    }

    out_dir = Path.home() / ".webloom" / "threads"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "workana.com.thread.json"
    out_path.write_text(json.dumps(thread, indent=2), encoding="utf-8")
    print(f"    wrote {out_path}")

    print(f"\n[workana-sandbox] complete. NO edits or proposals were submitted.")
    print(f"  login state: {'authenticated' if is_in else 'anonymous'}")
    print(f"  framework: {fw.get('primary')}")
    print(f"  anti-bot:  {ab.get('verdict')}")
    if not is_in:
        print(f"\n  To complete the sandbox, log in to Workana on this slot and re-run.")
    return 0


def main():
    p = argparse.ArgumentParser(description="WebLoom Workana sandbox")
    p.add_argument("--port", type=int, default=9226, help="Chrome debug port (slot-2 default)")
    p.add_argument("--auto", action="store_true", help="Use existing Workana tab if present")
    args = p.parse_args()
    sys.exit(asyncio.run(run_sandbox(args.port, args.auto)))


if __name__ == "__main__":
    main()
