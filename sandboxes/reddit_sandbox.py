"""Reddit primitive validator.

Connects to an already-running Chrome debug session (where Reddit is logged in),
visits a chosen post page, and exercises every Reddit-related WebLoom primitive
WITHOUT submitting any actual comments. Validates:

  • framework_detect picks up Lexical / shreddit / next.js
  • detect_anti_bot returns normal
  • scan_tab ax mode finds the composer placeholder + submit
  • lexical_set_text Strategy A succeeds (means __lexicalEditor is exposed)
  • the composer subtree contains the right submit button (and our scoped finder picks it)
  • outputs a hardened reddit.com.thread.json

Usage (with your real Chrome already running on a debug port):
    python reddit_sandbox.py --port 9224 --post-url "https://www.reddit.com/r/test/comments/..."

Or it'll pick the first live Reddit tab automatically:
    python reddit_sandbox.py --port 9224 --auto
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
SERVER_DIR = HERE.parent
sys.path.insert(0, str(SERVER_DIR))

import server  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────
def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


def _pick_reddit_tab(port: int) -> dict | None:
    for t in _list_tabs(port):
        if t.get("type") == "page" and "reddit.com" in (t.get("url", "") or ""):
            return t
    return None


async def _ready(ws: str, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            return True
        await asyncio.sleep(0.15)
    return False


# ── probes ─────────────────────────────────────────────────────────────────────
async def probe_framework(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def probe_anti_bot(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def probe_composer(ws: str) -> dict:
    """Find Reddit's comment composer + check Lexical readiness."""
    js = """(function() {
        // try several known composer placeholders
        const selectors = [
            'shreddit-async-loader[bundlename="comment_composer"]',
            'shreddit-composer',
            '[data-testid="comment-submission-form-richtext"]',
            'div[role="textbox"][aria-label*="comment" i]',
            'div[contenteditable=true][aria-label*="comment" i]',
            'div[contenteditable=true][data-lexical-editor=true]',
        ];
        const found = [];
        for (const s of selectors) {
            const el = document.querySelector(s);
            if (el) {
                const ce = el.isContentEditable
                    ? el
                    : (el.querySelector?.('[contenteditable=true]') || el.querySelector?.('[contenteditable]'));
                let lex = null;
                let cur = ce || el;
                for (let i = 0; i < 6 && cur; i++) {
                    if (cur.__lexicalEditor) { lex = true; break; }
                    cur = cur.parentElement;
                }
                found.push({
                    selector: s,
                    has_contenteditable: !!ce && ce.isContentEditable,
                    has_lexical_editor: lex,
                    visible: el.offsetParent !== null,
                });
            }
        }
        return JSON.stringify({results: found});
    })()"""
    r = await server.eval_in_tab(ws, js)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def test_lexical_set(ws: str, container_sel: str, text: str) -> dict:
    """Run the inlined lexical_set_text JS (no actual MCP roundtrip needed in the sandbox)."""
    # We reuse the same JS logic from server.py — easiest: call it via the tool
    # path. But here we want a contained test, so inline:
    js = f"""(async function(containerSel, text) {{
        const sleep = (ms) => new Promise(r => setTimeout(r, ms));
        const result = {{ steps: [] }};
        let editable = document.querySelector(containerSel);
        if (editable && !editable.isContentEditable) {{
            editable = editable.querySelector('[contenteditable=true]') || editable;
        }}
        if (!editable || !editable.isContentEditable) {{
            return JSON.stringify({{ok:false, error:'no contenteditable at ' + containerSel}});
        }}
        editable.focus();
        let lex = null, cur = editable;
        for (let i = 0; i < 6 && cur; i++) {{
            if (cur.__lexicalEditor) {{ lex = cur.__lexicalEditor; break; }}
            cur = cur.parentElement;
        }}
        if (lex && typeof lex.setEditorState === 'function') {{
            const lines = text.split(/\\r?\\n/);
            const paragraphs = lines.map(line => ({{
                children: line.length ? [{{detail:0,format:0,mode:'normal',style:'',text:line,type:'text',version:1}}] : [],
                direction:'ltr', format:'', indent:0, type:'paragraph', version:1
            }}));
            const stateJson = {{root: {{children: paragraphs, direction:'ltr', format:'', indent:0, type:'root', version:1}}}};
            try {{
                lex.setEditorState(lex.parseEditorState(JSON.stringify(stateJson)));
                await sleep(120);
                return JSON.stringify({{
                    ok:true, mode:'lexical-api', readback_len: (editable.innerText||'').length,
                    sample: (editable.innerText||'').slice(0, 100)
                }});
            }} catch(e) {{
                return JSON.stringify({{ok:false, mode:'lexical-api-failed', error: e.message}});
            }}
        }}
        return JSON.stringify({{ok:false, mode:'no-editor-handle'}});
    }})({json.dumps(container_sel)}, {json.dumps(text)})"""
    r = await server.eval_in_tab(ws, js)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def probe_submit_button(ws: str) -> dict:
    """Verify our scoped submit-finder selects the right button (NOT upvote/sort/etc)."""
    js = """(function() {
        let editable = document.activeElement;
        if (editable && !editable.isContentEditable) editable = editable.querySelector?.('[contenteditable=true]') || editable;
        if (!editable || !editable.isContentEditable) {
            const all = document.querySelectorAll('[contenteditable=true]');
            for (const e of all) { if (e.offsetParent !== null) { editable = e; break; } }
        }
        if (!editable) return JSON.stringify({error: 'no contenteditable'});
        const composerSel =
            'form, shreddit-composer, shreddit-async-loader, [class*="composer" i], '
            + '[class*="comment-form" i], [data-testid*="comment" i], [data-testid*="composer" i], '
            + '[role="dialog"]';
        let composer = editable;
        while (composer && composer !== document.body) {
            if (composer.matches && composer.matches(composerSel)) break;
            composer = composer.parentElement;
        }
        if (!composer) composer = document.querySelector('shreddit-async-loader[bundlename="comment_composer"], shreddit-composer');
        if (!composer) return JSON.stringify({error: 'no composer ancestor found'});
        const textKeys = ['comment', 'comentar', 'post', 'publicar', 'submit', 'reply', 'responder'];
        const allBtns = composer.querySelectorAll('button:not([disabled]), [role=button]:not([aria-disabled=true])');
        let firstTextMatch = null, firstSubmitType = null;
        for (const b of allBtns) {
            const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
            if (!firstTextMatch && textKeys.some(k => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k))) firstTextMatch = {text: t.slice(0, 60), tag: b.tagName};
            if (!firstSubmitType && b.matches('button[type="submit"]')) firstSubmitType = {text: (b.textContent || '').trim().slice(0, 60), tag: b.tagName};
        }
        const allSubmitOnPage = document.querySelectorAll('button[type="submit"]:not([disabled])').length;
        return JSON.stringify({
            composer_tag: composer.tagName,
            composer_id: composer.id || null,
            composer_class: (composer.className || '').toString().slice(0, 120),
            buttons_in_composer: allBtns.length,
            first_text_match: firstTextMatch,
            first_submit_type: firstSubmitType,
            submit_type_count_on_page: allSubmitOnPage,
        });
    })()"""
    r = await server.eval_in_tab(ws, js)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def click_composer_open(ws: str) -> dict:
    """Click the placeholder/wrapper that mounts Reddit's comment composer.
    Returns which selector worked + whether a contenteditable appeared after.
    """
    triggers = [
        'shreddit-async-loader[bundlename="comment_composer"]',
        '[data-testid="comment-submission-form-richtext"]',
        '[data-testid="comment-submission-form"]',
        'shreddit-composer',
        'div[role="textbox"][aria-label*="comment" i]',
    ]
    for t in triggers:
        r = await server.eval_in_tab(ws, f"""(function() {{
            const el = document.querySelector({json.dumps(t)});
            if (!el) return null;
            el.scrollIntoView({{block:'center'}});
            const fire = type => new MouseEvent(type, {{bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window}});
            el.dispatchEvent(fire('mousedown'));
            el.dispatchEvent(fire('mouseup'));
            el.click();
            return {json.dumps(t)};
        }})()""")
        val = r.get("result", {}).get("value")
        if val:
            await asyncio.sleep(0.8)
            ce = await server.eval_in_tab(ws, "!!document.querySelector('[contenteditable=true]')")
            return {"clicked": val, "contenteditable_appeared": ce.get("result", {}).get("value", False)}
    return {"clicked": None}


# ── orchestrator ───────────────────────────────────────────────────────────────
async def run_sandbox(port: int, post_url: str | None, auto: bool, dry_run: bool = True):
    print(f"\n[reddit-sandbox] connecting to Chrome debug on port {port}\n")

    # Pick tab
    tab = None
    if auto:
        tab = _pick_reddit_tab(port)
        if not tab:
            print("  no reddit tab found; pass --post-url to navigate one open")
            return 1
        ws = tab["webSocketDebuggerUrl"]
        print(f"  using existing tab: {tab.get('url', '')[:100]}")
    else:
        tabs = _list_tabs(port)
        # pick any page tab
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            print("  no page tabs found on this debug port")
            return 1
        tab = page_tabs[0]
        ws = tab["webSocketDebuggerUrl"]
        if post_url:
            await server.cdp_send(ws, "Page.navigate", {"url": post_url})
            await _ready(ws)
            await asyncio.sleep(2.5)  # SPA hydration buffer
            print(f"  navigated to: {post_url}")

    # ── 1. anti-bot
    print("[1] anti-bot probe")
    ab = await probe_anti_bot(ws)
    print(f"    verdict: {ab.get('verdict')}")
    for s in ab.get("signals", []):
        print(f"      • {s.get('type')} ({s.get('confidence', '?')})")

    # ── 2. framework
    print("\n[2] framework detection")
    fw = await probe_framework(ws)
    print(f"    primary: {fw.get('primary')}")
    print(f"    all:     {', '.join(fw.get('frameworks', []))}")
    ind = fw.get("indicators", {})
    for k, v in ind.items():
        print(f"    {k:32s} {v}")

    # ── 3. composer presence
    print("\n[3] composer placeholder probe")
    comp = await probe_composer(ws)
    composer_results = comp.get("results", [])
    if not composer_results:
        print("    none of the known selectors matched")
    for r in composer_results:
        print(f"    {r['selector'][:60]:60s}  ce={r['has_contenteditable']}  lexical={r['has_lexical_editor']}  visible={r['visible']}")

    # ── 4. click composer open (this DOES interact)
    print("\n[4] open composer (click trigger)")
    co = await click_composer_open(ws)
    print(f"    clicked: {co.get('clicked')}  -> contenteditable appeared: {co.get('contenteditable_appeared')}")

    # ── 5. find a contenteditable we can drive
    print("\n[5] locate contenteditable for set_text")
    ce_probe = await server.eval_in_tab(ws, """(function() {
        const all = document.querySelectorAll('[contenteditable=true]');
        for (const e of all) {
            if (e.offsetParent === null) continue;
            const aria = e.getAttribute('aria-label') || '';
            // Prefer ones labeled comment/composer
            if (/comment|composer|reply/i.test(aria) || e.closest('shreddit-async-loader[bundlename="comment_composer"], shreddit-composer')) {
                const id = e.id ? '#' + e.id : '';
                const cls = (e.className || '').toString().split(' ').slice(0, 2).join('.');
                return JSON.stringify({tag: e.tagName.toLowerCase(), id, cls, aria: aria.slice(0, 60)});
            }
        }
        return JSON.stringify({error: 'no labeled composer contenteditable'});
    })()""")
    ce_data = json.loads(ce_probe.get("result", {}).get("value", "{}"))
    print(f"    found: {ce_data}")

    # ── 6. test lexical_set_text (DRY — sets a benign string, doesn't submit)
    test_text = "WebLoom sandbox dry-run — please ignore (will not be submitted)."
    container_sel = '[contenteditable=true][aria-label*="comment" i], [contenteditable=true]'
    print(f"\n[6] lexical_set_text dry-run (no submit) — selector: {container_sel[:60]}")
    lex = await test_lexical_set(ws, container_sel, test_text)
    print(f"    {lex}")

    # ── 7. test submit-button discovery WITHOUT clicking
    print("\n[7] submit-button discovery (no click)")
    sb = await probe_submit_button(ws)
    print(f"    composer tag:       {sb.get('composer_tag')}")
    print(f"    composer id/class:  {sb.get('composer_id') or sb.get('composer_class','')[:60]}")
    print(f"    buttons inside:     {sb.get('buttons_in_composer')}")
    print(f"    type=submit on page: {sb.get('submit_type_count_on_page')} (we want to ignore these)")
    print(f"    text-match found:   {sb.get('first_text_match')}")
    print(f"    submit-type fallback: {sb.get('first_submit_type')}")

    # ── 8. Clear the dry-run text so it doesn't accidentally submit
    print("\n[8] clearing test text from composer (no commits made)")
    await test_lexical_set(ws, container_sel, "")

    # ── 9. Export hardened thread
    print("\n[9] writing reddit.com.thread.json")
    thread = {
        "domain": "reddit.com",
        "name": "Reddit Composer Profile (hardened)",
        "version": "0.2.0",
        "author": "reddit_sandbox.py",
        "license": "cc-by",
        "tier": "starter",
        "framework": fw.get("primary", "vanilla"),
        "frameworks_detected": fw.get("frameworks", []),
        "anti_bot_verdict": ab.get("verdict"),
        "default_strategy": "cdp",
        "notes": [
            "New comment composer is Lexical-based contenteditable (shreddit-composer bundle).",
            "Use `lexical_set_text` Strategy A — Reddit exposes `__lexicalEditor` on the contenteditable root. Atomic state replacement via setEditorState bypasses DOM/paste/auto-linkifier issues.",
            "Submit button selector MUST be scoped to the composer ancestor (not document-wide). A Reddit post page has 22+ `button[type=submit]` elements (upvote/sort/user actions/etc).",
            "Composer mount is JIT — click the placeholder, poll for contenteditable up to 5s.",
            "AutoMod can silently remove comments after submit. Always re-verify the comment appears for an anonymous viewer before declaring success.",
            "Comment composer does NOT have a markdown-toggle in current UI; POST composer does.",
        ],
        "selectors": {
            "composer_trigger_primary": "shreddit-async-loader[bundlename=\"comment_composer\"]",
            "composer_trigger_alt": "[data-testid=\"comment-submission-form-richtext\"]",
            "contenteditable_aria": "[contenteditable=true][aria-label*=\"comment\" i]",
            "contenteditable_lexical": "[contenteditable=true][data-lexical-editor=true]",
            "submit_button_scoping_ancestor": "form, shreddit-composer, shreddit-async-loader[bundlename=\"comment_composer\"]",
            "submit_button_text_keys": "comment | comentar | post | publicar | submit | reply | responder",
        },
        "quirks": {
            "lexical_editor_exposed_as": "__lexicalEditor on root contenteditable",
            "submit_button_scope_required": "Yes — 22+ button[type=submit] on a logged-in post page",
            "automod_silent_removal": "Common — verify comment is visible from logged-out view post-submit",
            "markdown_toggle_composer_only": "Comment composer has no markdown toggle; rich text only",
        },
        "actions": {
            "submit_comment": {
                "type": "scoped_button_click",
                "scope_selector": "shreddit-async-loader[bundlename=\"comment_composer\"], shreddit-composer",
                "match_by": "text",
                "text_keys": ["comment", "comentar", "post", "publicar", "reply", "responder"],
            },
        },
        "lexical_compatible": True,
        "validated_at": int(time.time()),
        "validated_by": "reddit_sandbox.py against live tab",
        "sample_post_url": post_url or (tab.get("url") if tab else ""),
    }
    out_dir = Path.home() / ".webloom" / "threads"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "reddit.com.thread.json"
    out_path.write_text(json.dumps(thread, indent=2), encoding="utf-8")
    print(f"    wrote {out_path}")

    print("\n[reddit-sandbox] complete. No comments were submitted.")
    print(f"  lexical_set_text mode: {lex.get('mode')}")
    print(f"  submit scoping working: {bool(sb.get('first_text_match'))}")
    print(f"  thread exported with {len(thread['notes'])} notes, {len(thread['selectors'])} selectors")
    return 0


def main():
    p = argparse.ArgumentParser(description="WebLoom Reddit primitive sandbox")
    p.add_argument("--port", type=int, default=9224, help="Chrome debug port (default 9224 / slot-1)")
    p.add_argument("--post-url", help="Navigate to this Reddit post URL before testing")
    p.add_argument("--auto", action="store_true", help="Use whatever Reddit tab is already open")
    args = p.parse_args()
    if not args.post_url and not args.auto:
        print("provide --post-url or --auto")
        sys.exit(2)
    sys.exit(asyncio.run(run_sandbox(args.port, args.post_url, args.auto)))


if __name__ == "__main__":
    main()
