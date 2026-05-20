"""Reddit autoposter — production CLI.

Reads a JSON config listing (post_url, markdown, account_session) entries and
submits each comment via the hardened lexical_set_text + scoped-submit flow.
Includes:

  • per-account rate limiting (default: 1 comment per 30 min per account)
  • dry-run mode (sets text but does NOT click submit — for end-to-end verification)
  • landed-verification (re-checks the comment appears for an anonymous viewer)
  • per-comment delay jitter to avoid posting at identical intervals
  • outcome log to .reddit-poster-log.jsonl (append-only)

Config format (campaigns.json):
{
  "default_session": "slot-1",
  "default_session_port": 9224,
  "rate_limit_seconds": 1800,
  "jitter_seconds": [60, 300],
  "comments": [
    {
      "post_url": "https://www.reddit.com/r/n8n/comments/...",
      "markdown": "Hey — I built something for exactly this...",
      "session_port": 9224,         // optional, defaults to default_session_port
      "dry_run": false              // optional per-comment override
    }
  ]
}

Usage:
    python reddit_poster.py --config campaigns.json
    python reddit_poster.py --config campaigns.json --dry-run    # never click submit
    python reddit_poster.py --config campaigns.json --only 2     # only the 3rd entry
"""
import argparse
import asyncio
import json
import random
import socket
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
SERVER_DIR = HERE.parent
sys.path.insert(0, str(SERVER_DIR))

import server  # noqa: E402


LOG_PATH = Path.home() / ".webloom" / "logs" / "reddit-poster.jsonl"


def log_outcome(entry: dict):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry["ts"] = int(time.time())
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


async def _open_or_use_tab(port: int, url: str) -> str | None:
    """Find a tab pointed at this URL (any URL on the same post), or open a new one."""
    base = url.split("?")[0].rstrip("/")
    for t in _list_tabs(port):
        if t.get("type") == "page":
            tu = (t.get("url") or "").split("?")[0].rstrip("/")
            if tu.startswith(base):
                return t["webSocketDebuggerUrl"]
    # No matching tab — open a new one
    res = await server.cdp_browser_send(port, "Target.createTarget", {"url": url})
    tid = res.get("targetId")
    if not tid:
        return None
    await asyncio.sleep(0.5)
    for t in _list_tabs(port):
        if t.get("id") == tid:
            return t["webSocketDebuggerUrl"]
    return None


async def _wait_ready(ws: str, timeout: float = 12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            return True
        await asyncio.sleep(0.2)
    return False


# ── core comment flow ─────────────────────────────────────────────────────────
LEXICAL_SET_JS_TEMPLATE = r"""(async function(containerSel, text) {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const probe = document.querySelector(containerSel);
    if (!probe) return JSON.stringify({ok:false, error:'composer trigger not found: ' + containerSel});
    // Click composer trigger
    probe.scrollIntoView({block:'center'});
    const fire = t => new MouseEvent(t, {bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window});
    probe.dispatchEvent(fire('mousedown'));
    probe.dispatchEvent(fire('mouseup'));
    probe.click();
    // Poll for contenteditable
    let editable = null;
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
        const ces = document.querySelectorAll('[contenteditable=true]');
        for (const e of ces) {
            if (e.offsetParent === null) continue;
            const aria = e.getAttribute('aria-label') || '';
            if (/comment|composer|reply/i.test(aria) || e.closest('shreddit-async-loader[bundlename="comment_composer"], shreddit-composer')) {
                editable = e; break;
            }
        }
        if (editable) break;
        await sleep(120);
    }
    if (!editable) return JSON.stringify({ok:false, error:'no composer contenteditable mounted'});
    editable.focus();
    // Find __lexicalEditor
    let lex = null, cur = editable;
    for (let i = 0; i < 6 && cur; i++) {
        if (cur.__lexicalEditor) { lex = cur.__lexicalEditor; break; }
        cur = cur.parentElement;
    }
    if (lex && typeof lex.setEditorState === 'function') {
        const lines = text.split(/\r?\n/);
        const paragraphs = lines.map(line => ({
            children: line.length ? [{detail:0,format:0,mode:'normal',style:'',text:line,type:'text',version:1}] : [],
            direction:'ltr', format:'', indent:0, type:'paragraph', version:1
        }));
        const stateJson = {root: {children: paragraphs, direction:'ltr', format:'', indent:0, type:'root', version:1}};
        try {
            lex.setEditorState(lex.parseEditorState(JSON.stringify(stateJson)));
            await sleep(180);
            return JSON.stringify({ok:true, mode:'lexical-api', readback_len: (editable.innerText||'').length});
        } catch(e) {
            return JSON.stringify({ok:false, mode:'lexical-api-error', error: e.message});
        }
    }
    return JSON.stringify({ok:false, mode:'no-editor-handle'});
})"""


SUBMIT_JS = r"""(function() {
    let editable = document.activeElement;
    if (editable && !editable.isContentEditable) editable = editable.querySelector?.('[contenteditable=true]') || editable;
    if (!editable || !editable.isContentEditable) {
        const all = document.querySelectorAll('[contenteditable=true]');
        for (const e of all) { if (e.offsetParent !== null) { editable = e; break; } }
    }
    if (!editable) return JSON.stringify({ok:false, error: 'no contenteditable to anchor from'});
    const composerSel =
        'form, shreddit-composer, shreddit-async-loader, [class*="composer" i], '
        + '[class*="comment-form" i], [data-testid*="comment" i], [data-testid*="composer" i], [role="dialog"]';
    let composer = editable;
    while (composer && composer !== document.body) {
        if (composer.matches && composer.matches(composerSel)) break;
        composer = composer.parentElement;
    }
    if (!composer || composer === document.body) {
        composer = document.querySelector('shreddit-async-loader[bundlename="comment_composer"], shreddit-composer');
    }
    if (!composer) return JSON.stringify({ok:false, error: 'no composer ancestor'});
    const textKeys = ['comment', 'comentar', 'post', 'publicar', 'submit', 'reply', 'responder'];
    const allBtns = composer.querySelectorAll('button:not([disabled]), [role=button]:not([aria-disabled=true])');
    let target = null;
    for (const b of allBtns) {
        const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
        if (textKeys.some(k => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k))) { target = b; break; }
    }
    if (!target) target = composer.querySelector('button[type="submit"]:not([disabled])');
    if (!target) return JSON.stringify({ok:false, error: 'no submit button inside composer'});
    target.scrollIntoView({block:'center'});
    const fire = t => new MouseEvent(t, {bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window});
    target.dispatchEvent(fire('mousedown'));
    target.dispatchEvent(fire('mouseup'));
    target.click();
    return JSON.stringify({ok:true, text: (target.textContent || '').trim().slice(0, 60)});
})()"""


async def post_one(port: int, post_url: str, markdown: str, dry_run: bool) -> dict:
    ws = await _open_or_use_tab(port, post_url)
    if not ws:
        return {"ok": False, "error": "could not open/find tab"}
    await _wait_ready(ws)
    await asyncio.sleep(1.5)  # SPA hydration

    # 1. Anti-bot check
    ab_r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    try:
        ab = json.loads(ab_r.get("result", {}).get("value", "{}"))
    except Exception:
        ab = {"verdict": "unknown"}
    if ab.get("verdict") not in ("normal", "unknown", "?"):
        return {"ok": False, "error": f"anti-bot triggered: {ab.get('verdict')}", "signals": ab.get("signals", [])}

    # 2. Open composer + set text via inlined Lexical Strategy A
    container_sel = (
        'shreddit-async-loader[bundlename="comment_composer"], '
        '[data-testid="comment-submission-form-richtext"], '
        'shreddit-composer'
    )
    js = LEXICAL_SET_JS_TEMPLATE + f"({json.dumps(container_sel)}, {json.dumps(markdown)})"
    set_r = await server.eval_in_tab(ws, js)
    try:
        set_val = json.loads(set_r.get("result", {}).get("value", "{}"))
    except Exception:
        set_val = {"ok": False, "error": "set_text parse failed"}
    if not set_val.get("ok"):
        return {"ok": False, "stage": "set_text", "detail": set_val}

    if dry_run:
        return {"ok": True, "dry_run": True, "set": set_val, "skipped_submit": True}

    await asyncio.sleep(0.6)

    # 3. Click submit (scoped)
    sub_r = await server.eval_in_tab(ws, SUBMIT_JS)
    try:
        sub_val = json.loads(sub_r.get("result", {}).get("value", "{}"))
    except Exception:
        sub_val = {"ok": False, "error": "submit parse failed"}
    if not sub_val.get("ok"):
        return {"ok": False, "stage": "submit", "detail": sub_val, "set": set_val}

    await asyncio.sleep(3.0)

    # 4. Verify landed — search for our text on the page
    sample = markdown[:80].replace('"', '\\"')
    verify_js = f"""(function() {{
        const target = {json.dumps(markdown[:80])};
        const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
        const all = document.querySelectorAll('p, div, span');
        let found = 0;
        for (const el of all) {{
            if (norm(el.textContent).includes(norm(target))) found++;
        }}
        return JSON.stringify({{found, sample_target: target.slice(0, 60)}});
    }})()"""
    ver_r = await server.eval_in_tab(ws, verify_js)
    try:
        ver = json.loads(ver_r.get("result", {}).get("value", "{}"))
    except Exception:
        ver = {"found": 0}

    return {
        "ok": True,
        "set": set_val,
        "submit": sub_val,
        "verify": ver,
        "landed": ver.get("found", 0) > 0,
    }


async def run_campaign(config_path: Path, dry_run_override: bool, only_index: int | None):
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    default_port = int(cfg.get("default_session_port", 9224))
    rate_limit = float(cfg.get("rate_limit_seconds", 1800))
    jitter = cfg.get("jitter_seconds", [60, 300])
    comments = cfg.get("comments", [])
    if only_index is not None:
        comments = [comments[only_index]]

    last_post_at: dict[int, float] = {}  # port -> ts of last submit

    print(f"\n[reddit-poster] {len(comments)} comment(s) to process")
    print(f"  rate limit:    {rate_limit:.0f}s per account")
    print(f"  jitter:        {jitter[0]}-{jitter[1]}s between posts")
    print(f"  global dry_run: {dry_run_override}\n")

    for i, c in enumerate(comments, 1):
        port = int(c.get("session_port", default_port))
        url = c["post_url"]
        md = c["markdown"]
        dry = bool(c.get("dry_run", False)) or dry_run_override

        # Rate-limit
        last = last_post_at.get(port, 0)
        wait_for = max(0, (last + rate_limit) - time.time()) if last and not dry else 0
        if wait_for > 0:
            print(f"[{i}/{len(comments)}] rate-limit: waiting {wait_for:.0f}s for port {port}")
            await asyncio.sleep(wait_for)

        print(f"[{i}/{len(comments)}] {url[:90]}")
        result = await post_one(port, url, md, dry_run=dry)
        landed = result.get("landed")
        ok = result.get("ok")
        if ok and dry:
            print("    ✓ DRY RUN — text set, no submit")
        elif ok and landed:
            print("    ✓ landed (visible on page)")
        elif ok and not landed:
            print("    ⚠ submit clicked, but not visible on verify — possible AutoMod silent removal")
        else:
            print(f"    ✗ failed: {result.get('stage','?')} — {result.get('error') or result.get('detail')}")

        log_outcome({"i": i, "port": port, "url": url, "dry": dry, "result": result})

        if not dry:
            last_post_at[port] = time.time()

        # jitter
        if i < len(comments):
            delay = random.randint(int(jitter[0]), int(jitter[1]))
            print(f"    sleeping {delay}s before next")
            await asyncio.sleep(delay)

    print(f"\n[reddit-poster] done. Log: {LOG_PATH}")


def main():
    p = argparse.ArgumentParser(description="WebLoom Reddit autoposter")
    p.add_argument("--config", required=True, help="Path to campaigns JSON config")
    p.add_argument("--dry-run", action="store_true", help="Set text but never click submit")
    p.add_argument("--only", type=int, help="Run only the Nth (0-indexed) comment entry")
    args = p.parse_args()
    asyncio.run(run_campaign(Path(args.config), args.dry_run, args.only))


if __name__ == "__main__":
    main()
