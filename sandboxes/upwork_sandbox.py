"""Upwork profile-edit sandbox.

Connects to a Chrome debug session where Upwork is already logged in. Probes the
profile editing UI to identify framework, editor types, selectors, and primitives
needed per section. NEVER saves any changes — opens modals, captures structure,
closes via Cancel/X.

Output: hardened ~/.webloom/threads/upwork.com.thread.json + a recipe MARSTUDIO
can replay to edit the profile fast.

Usage:
    python upwork_sandbox.py --port 9226 --auto                  # use whatever tab is open
    python upwork_sandbox.py --port 9226 --profile-url <url>     # navigate to specific edit URL
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


def _pick_upwork_tab(port: int) -> dict | None:
    for t in _list_tabs(port):
        if t.get("type") == "page" and "upwork.com" in (t.get("url", "") or ""):
            return t
    return None


async def _ready(ws: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            return True
        await asyncio.sleep(0.2)
    return False


# ── probes ─────────────────────────────────────────────────────────────────────
async def probe_framework(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


async def probe_anti_bot(ws: str) -> dict:
    r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


PROFILE_EDIT_TRIGGERS_JS = r"""(function() {
    // Upwork sprinkles "edit" pencil/button affordances next to each section.
    // Identify visible edit triggers + their context (section name)
    const triggers = [];
    const allEditable = document.querySelectorAll('[aria-label*="edit" i], button[aria-label*="edit" i], [data-test*="edit" i], [data-qa*="edit" i]');
    for (const el of allEditable) {
        if (el.offsetParent === null) continue;  // skip hidden
        const aria = el.getAttribute('aria-label') || el.textContent || '';
        const tag = el.tagName.toLowerCase();
        const dataTest = el.getAttribute('data-test') || el.getAttribute('data-qa') || '';
        // Walk up to find section heading for context
        let cur = el, section = '';
        for (let i = 0; i < 8 && cur; i++) {
            const heading = cur.querySelector?.('h1, h2, h3, h4');
            if (heading && heading.offsetParent !== null) {
                section = heading.textContent.trim().slice(0, 80);
                break;
            }
            cur = cur.parentElement;
        }
        const r = el.getBoundingClientRect();
        triggers.push({
            tag,
            aria: aria.trim().slice(0, 80),
            data_test: dataTest,
            section: section,
            y: Math.round(r.top + window.scrollY),
            visible: r.width > 0 && r.height > 0,
        });
    }
    return JSON.stringify({triggers, url: location.href, title: document.title});
})()"""


async def probe_edit_triggers(ws: str) -> dict:
    r = await server.eval_in_tab(ws, PROFILE_EDIT_TRIGGERS_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


MODAL_PROBE_JS = r"""(function() {
    // After clicking an edit trigger Upwork opens a modal. Probe its structure:
    // editor types, input fields, save button, cancel button.
    const findRoleModal = () => {
        const candidates = document.querySelectorAll('[role=dialog], [aria-modal=true], .air3-modal, [class*="modal" i][class*="open" i]');
        for (const c of candidates) {
            if (c.offsetParent !== null) return c;
        }
        return null;
    };
    const modal = findRoleModal();
    if (!modal) return JSON.stringify({error: 'no visible modal'});

    const header = modal.querySelector('h1, h2, h3, .air3-modal-header, [class*="header" i]')?.textContent?.trim().slice(0, 100) || '';

    // Detect editor type inside modal
    const lexicalCE = modal.querySelector('[contenteditable=true][data-lexical-editor=true]');
    const proseMirrorCE = modal.querySelector('[contenteditable=true].ProseMirror, .ProseMirror[contenteditable=true]');
    const draftJsCE = modal.querySelector('[contenteditable=true].public-DraftEditor-content');
    const quillCE = modal.querySelector('[contenteditable=true].ql-editor');
    const plainCE = modal.querySelector('[contenteditable=true]:not(.ProseMirror):not(.ql-editor):not(.public-DraftEditor-content):not([data-lexical-editor=true])');
    const editorType =
        lexicalCE ? 'lexical' :
        proseMirrorCE ? 'prosemirror' :
        draftJsCE ? 'draft-js' :
        quillCE ? 'quill' :
        plainCE ? 'plain-contenteditable' :
        'none';

    // Map all inputs
    const inputs = Array.from(modal.querySelectorAll('input, textarea, select')).map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.type || '',
        name: el.name || '',
        id: el.id || '',
        placeholder: el.placeholder || '',
        aria: el.getAttribute('aria-label') || '',
        autocomplete: el.getAttribute('autocomplete') || '',
        readonly: el.readOnly || el.disabled || false,
    })).slice(0, 30);

    // Find save + cancel buttons
    const allBtns = Array.from(modal.querySelectorAll('button:not([disabled]), [role=button]:not([aria-disabled=true])'));
    const saveKeys = ['save', 'guardar', 'submit', 'enviar', 'update', 'actualizar', 'apply', 'aplicar'];
    const cancelKeys = ['cancel', 'cancelar', 'close', 'cerrar', 'dismiss'];
    let saveBtn = null, cancelBtn = null;
    for (const b of allBtns) {
        const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
        if (!saveBtn && saveKeys.some(k => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k))) {
            saveBtn = {text: t.slice(0, 60), classes: (b.className||'').toString().slice(0,80), data_test: b.getAttribute('data-test') || b.getAttribute('data-qa') || ''};
        }
        if (!cancelBtn && cancelKeys.some(k => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k))) {
            cancelBtn = {text: t.slice(0, 60), classes: (b.className||'').toString().slice(0,80), data_test: b.getAttribute('data-test') || b.getAttribute('data-qa') || ''};
        }
    }

    // Detect autocomplete dropdowns (skills, languages, etc.)
    const comboboxes = Array.from(modal.querySelectorAll('[role=combobox], [aria-haspopup="listbox"], input[aria-autocomplete]')).map(el => ({
        tag: el.tagName.toLowerCase(),
        aria: el.getAttribute('aria-label') || '',
        autocomplete: el.getAttribute('aria-autocomplete') || '',
        haspopup: el.getAttribute('aria-haspopup') || '',
    }));

    return JSON.stringify({
        modal_tag: modal.tagName,
        modal_class: (modal.className||'').toString().slice(0, 120),
        modal_data_test: modal.getAttribute('data-test') || modal.getAttribute('data-qa') || '',
        header,
        editor_type: editorType,
        input_count: inputs.length,
        inputs: inputs.slice(0, 12),
        comboboxes,
        save_btn: saveBtn,
        cancel_btn: cancelBtn,
        all_button_count: allBtns.length,
    });
})()"""


async def probe_modal(ws: str) -> dict:
    r = await server.eval_in_tab(ws, MODAL_PROBE_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


CLOSE_MODAL_JS = r"""(function() {
    // Close the open modal via cancel/close/escape — NEVER click save during probing
    const findRoleModal = () => {
        const candidates = document.querySelectorAll('[role=dialog], [aria-modal=true], .air3-modal');
        for (const c of candidates) {
            if (c.offsetParent !== null) return c;
        }
        return null;
    };
    const modal = findRoleModal();
    if (!modal) return JSON.stringify({closed: 'no_modal'});

    // Try a close button (X icon or "Cancel")
    const allBtns = Array.from(modal.querySelectorAll('button:not([disabled]), [role=button]:not([aria-disabled=true])'));
    const cancelKeys = ['cancel', 'cancelar', 'close', 'cerrar', 'dismiss'];
    let target = null;
    for (const b of allBtns) {
        const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
        if (cancelKeys.some(k => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k))) { target = b; break; }
    }
    // Fallback: find an X-icon button (close icon usually has aria-label="close" or is in modal header)
    if (!target) {
        const xBtn = modal.querySelector('button[aria-label*="close" i], button[aria-label*="cerrar" i], button.air3-btn-close');
        if (xBtn) target = xBtn;
    }
    if (target) {
        const fire = t => new MouseEvent(t, {bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window});
        target.dispatchEvent(fire('mousedown'));
        target.dispatchEvent(fire('mouseup'));
        target.click();
        return JSON.stringify({closed: 'button', text: (target.textContent || '').trim().slice(0, 60)});
    }
    // Last resort: ESC key
    document.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'Escape', code:'Escape', keyCode:27}));
    return JSON.stringify({closed: 'esc'});
})()"""


async def close_modal(ws: str):
    r = await server.eval_in_tab(ws, CLOSE_MODAL_JS)
    return json.loads(r.get("result", {}).get("value", "{}"))


# ── orchestrator ───────────────────────────────────────────────────────────────
async def run_sandbox(port: int, profile_url: str | None, auto: bool, max_sections: int):
    print(f"\n[upwork-sandbox] connecting to Chrome debug on port {port}\n")

    tabs = _list_tabs(port)
    ws = None
    if auto:
        t = _pick_upwork_tab(port)
        if not t:
            print("  no upwork tab found; pass --profile-url to navigate")
            return 1
        ws = t["webSocketDebuggerUrl"]
        print(f"  using existing tab: {t.get('url','')[:100]}")
    else:
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            print("  no page tabs found")
            return 1
        ws = page_tabs[0]["webSocketDebuggerUrl"]
        if profile_url:
            await server.cdp_send(ws, "Page.navigate", {"url": profile_url})
            await _ready(ws)
            await asyncio.sleep(2.5)
            print(f"  navigated to: {profile_url}")

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

    # ── 3. find edit triggers on current page
    print("\n[3] scanning for edit triggers on current page")
    tr = await probe_edit_triggers(ws)
    triggers = tr.get("triggers", [])
    print(f"    {len(triggers)} edit triggers found on: {tr.get('url','')[:80]}")
    for t in triggers[:max_sections]:
        print(f"      - section='{t.get('section') or '?'}'  aria='{t.get('aria')}'  data-test='{t.get('data_test')}'")

    if not triggers:
        print("\n    No edit triggers visible. Make sure you're on the profile page that has edit pencils.")
        print("    Common Upwork URLs: /freelancers/~<id>  or  /freelancers/settings/profile")
        return 1

    # ── 4. for each visible trigger, click → probe modal → close modal
    print(f"\n[4] probing modals (up to {max_sections} sections)")
    section_results = []
    for i, t in enumerate(triggers[:max_sections]):
        section_label = t.get('section') or t.get('aria') or f'section_{i}'
        print(f"\n  [{i+1}/{min(len(triggers), max_sections)}] probing: \"{section_label}\"")

        # Click the trigger via aria/data-test selector
        click_js = f"""(function() {{
            const target_aria = {json.dumps(t.get('aria', '').lower())};
            const target_dt = {json.dumps(t.get('data_test', ''))};
            const all = document.querySelectorAll('button, [role=button], [data-test], [data-qa]');
            for (const el of all) {{
                if (el.offsetParent === null) continue;
                const a = (el.getAttribute('aria-label') || el.textContent || '').trim().toLowerCase();
                const dt = (el.getAttribute('data-test') || el.getAttribute('data-qa') || '');
                if ((target_dt && dt === target_dt) || (target_aria && a === target_aria)) {{
                    el.scrollIntoView({{block:'center'}});
                    const fire = t => new MouseEvent(t, {{bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window}});
                    el.dispatchEvent(fire('mousedown'));
                    el.dispatchEvent(fire('mouseup'));
                    el.click();
                    return JSON.stringify({{clicked: a || dt}});
                }}
            }}
            return JSON.stringify({{error: 'trigger not re-located'}});
        }})()"""
        click_r = await server.eval_in_tab(ws, click_js)
        click_v = json.loads(click_r.get("result", {}).get("value", "{}"))
        if click_v.get("error"):
            print(f"      could not re-find trigger: {click_v.get('error')}")
            continue

        # Wait for modal to render
        await asyncio.sleep(1.2)

        # Probe modal structure
        mp = await probe_modal(ws)
        if mp.get("error"):
            print(f"      no modal opened (or invisible): {mp.get('error')}")
            continue

        print(f"      modal header: \"{mp.get('header')}\"")
        print(f"      editor type:  {mp.get('editor_type')}")
        print(f"      inputs: {mp.get('input_count')}   comboboxes: {len(mp.get('comboboxes', []))}")
        if mp.get("save_btn"):
            print(f"      save btn: \"{mp['save_btn'].get('text')}\"")
        if mp.get("cancel_btn"):
            print(f"      cancel btn: \"{mp['cancel_btn'].get('text')}\"")

        section_results.append({
            "section_label": section_label,
            "trigger_aria": t.get("aria"),
            "trigger_data_test": t.get("data_test"),
            "modal": mp,
        })

        # Close modal — never save
        cl = await close_modal(ws)
        print(f"      closed via: {cl.get('closed')}")
        await asyncio.sleep(0.6)

    # ── 5. export hardened thread
    print("\n[5] writing upwork.com.thread.json")

    # Aggregate editor types observed
    editor_types_seen = set()
    for s in section_results:
        et = s.get("modal", {}).get("editor_type")
        if et and et != "none":
            editor_types_seen.add(et)

    notes = [
        "Profile editing uses MODAL-based flows — each section opens a separate dialog with [role=dialog].",
        f"Edit triggers found via [aria-label*=edit i], button[aria-label*=edit i], [data-test*=edit i].",
        f"{len(triggers)} edit triggers visible on the profile page tested.",
        "Modals close via Cancel/Cerrar button or Escape key — confirmed safe pattern.",
    ]
    if "prosemirror" in editor_types_seen:
        notes.append("ProseMirror editor detected for rich-text sections — needs custom set-text approach (Lexical primitive won't work). Plan: use focus + selectAll + delete + paste via ClipboardEvent with text/plain DataTransfer.")
    if "lexical" in editor_types_seen:
        notes.append("Lexical editor detected — `lexical_set_text` Strategy A works (atomic state set via __lexicalEditor.setEditorState).")
    if "quill" in editor_types_seen:
        notes.append("Quill editor detected — use the Quill API via window: `editor.setText(text)` if accessible, else paste-event fallback.")
    if "draft-js" in editor_types_seen:
        notes.append("Draft.js detected — similar to Lexical pattern, but state set via Editor.onChange handler.")
    if "plain-contenteditable" in editor_types_seen:
        notes.append("Plain contenteditable found — works with native selectAll+delete+paste pattern.")

    save_buttons = []
    for s in section_results:
        sb = s.get("modal", {}).get("save_btn")
        if sb:
            save_buttons.append({"section": s.get("section_label"), "text": sb.get("text"), "data_test": sb.get("data_test")})

    thread = {
        "domain": "upwork.com",
        "name": "Upwork Profile Editor (hardened)",
        "version": "0.1.0",
        "author": "upwork_sandbox.py",
        "license": "cc-by",
        "tier": "pro",
        "framework": fw.get("primary", "vanilla"),
        "frameworks_detected": fw.get("frameworks", []),
        "anti_bot_verdict": ab.get("verdict"),
        "default_strategy": "cdp",
        "notes": notes,
        "selectors": {
            "edit_triggers": "[aria-label*='edit' i], button[aria-label*='edit' i], [data-test*='edit' i], [data-qa*='edit' i]",
            "modal_container": "[role=dialog], [aria-modal=true], .air3-modal",
            "modal_header": "h1, h2, h3, .air3-modal-header",
            "save_button_text_keys": "save | guardar | submit | enviar | update | actualizar | apply | aplicar",
            "cancel_button_text_keys": "cancel | cancelar | close | cerrar | dismiss",
            "close_x_button": "button[aria-label*='close' i], button[aria-label*='cerrar' i], button.air3-btn-close",
        },
        "quirks": {
            "modal_save_scope_required": "Like Reddit — many submit-like buttons on a logged-in profile page; ALWAYS scope to [role=dialog] before searching for save",
            "editor_types_seen": sorted(editor_types_seen) or ["none-detected-in-probed-sections"],
            "skill_autocomplete": "Skills use [role=combobox] with aria-autocomplete; type via key_type then click matching listbox option",
        },
        "edit_sections": section_results,
        "validated_at": int(time.time()),
        "validated_by": "upwork_sandbox.py (read-only, no saves)",
        "tested_url": tr.get("url", ""),
    }
    out_dir = Path.home() / ".webloom" / "threads"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "upwork.com.thread.json"
    out_path.write_text(json.dumps(thread, indent=2), encoding="utf-8")
    print(f"    wrote {out_path}")

    print(f"\n[upwork-sandbox] complete. NO edits were saved.")
    print(f"  sections probed: {len(section_results)}")
    print(f"  editor types found: {sorted(editor_types_seen) or ['none']}")
    print(f"  recipe for MARSTUDIO ready in the Thread file.")
    return 0


def main():
    p = argparse.ArgumentParser(description="WebLoom Upwork profile-edit sandbox")
    p.add_argument("--port", type=int, default=9226, help="Chrome debug port (slot-2 default)")
    p.add_argument("--profile-url", help="Navigate to this profile URL first (e.g. /freelancers/~<id>)")
    p.add_argument("--auto", action="store_true", help="Use whatever Upwork tab is already open")
    p.add_argument("--max-sections", type=int, default=8, help="Limit number of edit sections to probe")
    args = p.parse_args()
    if not args.profile_url and not args.auto:
        print("provide --profile-url or --auto")
        sys.exit(2)
    sys.exit(asyncio.run(run_sandbox(args.port, args.profile_url, args.auto, args.max_sections)))


if __name__ == "__main__":
    main()
