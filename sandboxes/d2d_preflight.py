"""D2D pre-flight check.

Quick validator (~2-3 min) run BEFORE a real D2D publish. Confirms that every
selector + primitive cracked on prior sessions still works against the current
Draft2Digital UI.

Uses the existing bookshelf page — does NOT create any draft book. Probes:
  - Bookshelf / Add New Book button
  - Author dropdown (custom React Select listening on mousedown)
  - Strategy D file upload pattern (label-wrapped hidden input)
  - Approval checkbox
  - SUBMIT button text-match

NEVER saves, modifies, or submits anything.

Usage:
    python d2d_preflight.py --port 9226
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


# Per-page checks. Same kinds as KDP preflight.
CHECKS_BOOKSHELF = [
    ("react_16_marker",         "framework_marker", "react-16",                                "react-16 detected (D2D's framework)"),
    ("add_new_book_button",     "selector_exists",  "#add-book, a.my-books, [class*='add-book' i]", "Add New Book entry"),
    ("collapse_all_button",     "selector_exists",  "#collapse-all-button",                    "Collapse All in bookshelf"),
    ("my_books_link",           "button_text",      "my books",                                "MY BOOKS nav link"),
]

CHECKS_EBOOK_FORM_PRE_START = [
    # Checks valid BEFORE Start eBook is clicked — book is on bookshelf, ebook wizard not yet entered
    ("start_ebook_button",      "selector_exists",  "#start-ebook-button",                     "Start eBook button (gates entry into wizard)"),
    ("book_volume_input",       "selector_exists",  "#volumeNumber",                           "Series volume input"),
    ("bisac_filter_input",      "selector_exists",  "#filter-bisacs",                          "BISAC search/filter input"),
    ("react_16_marker",         "framework_marker", "react-16",                                "react-16 detected (D2D's framework)"),
]

CHECKS_EBOOK_FORM_POST_START = [
    # Only valid AFTER Start eBook clicked — full wizard form rendered
    ("author_combobox",         "selector_exists",  "#authorName",                             "Choose Author combobox"),
    ("author_combobox_role",    "js_check",         "document.querySelector('#authorName')?.getAttribute('role') === 'combobox'",  "role=combobox attribute"),
    ("approval_checkbox_text",  "page_text",        "approve",                                  "approval text present (was 'reviewed/approve/release')"),
    ("save_and_continue_btn",   "button_text",      "save",                                    "save/continue button present"),
    ("submit_button_text",      "button_text",      "submit",                                  "submit button"),
    ("file_input_label_wrap",   "selector_exists",  "label input[type=file], label input[type=file][hidden]",  "label-wrapped hidden file input (Strategy D target)"),
]


# ── helpers (same shape as KDP preflight) ─────────────────────────────────────
def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


def _pick_d2d_tab(port: int) -> dict | None:
    for t in _list_tabs(port):
        if t.get("type") == "page" and "draft2digital.com" in (t.get("url", "") or ""):
            return t
    return None


async def _open_or_find_tab(port: int) -> str:
    t = _pick_d2d_tab(port)
    if t:
        return t["webSocketDebuggerUrl"]
    for t in _list_tabs(port):
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


async def _check_selector_exists(ws: str, sel: str) -> bool:
    r = await server.eval_in_tab(ws, f"!!document.querySelector({json.dumps(sel)})")
    return bool(r.get("result", {}).get("value"))


async def _check_button_text(ws: str, text_lower: str) -> bool:
    js = f"""(function() {{
        const target = {json.dumps(text_lower)};
        const all = document.querySelectorAll('button, [role=button], a');
        for (const b of all) {{
            if (b.offsetParent === null) continue;
            const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
            if (t === target || t.includes(target)) return true;
        }}
        return false;
    }})()"""
    r = await server.eval_in_tab(ws, js)
    return bool(r.get("result", {}).get("value"))


async def _check_page_text(ws: str, text_substr: str) -> bool:
    js = f"""(document.body.innerText || '').toLowerCase().includes({json.dumps(text_substr.lower())})"""
    r = await server.eval_in_tab(ws, js)
    return bool(r.get("result", {}).get("value"))


async def _check_js(ws: str, expr: str) -> bool:
    r = await server.eval_in_tab(ws, f"!!({expr})")
    return bool(r.get("result", {}).get("value"))


async def _check_framework_marker(ws: str, expected: str) -> tuple[bool, str]:
    r = await server.eval_in_tab(ws, server.FRAMEWORK_DETECT_JS)
    try:
        data = json.loads(r.get("result", {}).get("value", "{}"))
        fws = data.get("frameworks", [])
        return any(expected in f for f in fws), f"detected={fws}"
    except Exception:
        return False, "framework_detect parse failed"


async def run_checks(ws: str, step_name: str, checks: list) -> tuple[int, int, list[str]]:
    passed = 0
    failed = 0
    failures = []
    print(f"\n[{step_name}]")
    for name, kind, probe, expected in checks:
        ok = False
        detail = ""
        try:
            if kind == "selector_exists":
                ok = await _check_selector_exists(ws, probe)
            elif kind == "button_text":
                ok = await _check_button_text(ws, probe)
            elif kind == "page_text":
                ok = await _check_page_text(ws, probe)
            elif kind == "js_check":
                ok = await _check_js(ws, probe)
            elif kind == "framework_marker":
                ok, detail = await _check_framework_marker(ws, probe)
        except Exception as e:
            ok = False
            detail = f"ERROR: {e}"
        status = "✓" if ok else "✗"
        print(f"  {status} {name:30s} — {expected}{(' · ' + detail) if detail and not ok else ''}")
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append(f"  • {step_name}/{name}: expected `{expected}`")
    return passed, failed, failures


async def run_preflight(port: int):
    print(f"\n[d2d-preflight] connecting to Chrome on port {port}")
    ws = await _open_or_find_tab(port)
    print(f"  connected to tab")

    # Anti-bot
    print("\n[0] anti-bot probe")
    ab_r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    try:
        ab = json.loads(ab_r.get("result", {}).get("value", "{}"))
        print(f"    verdict: {ab.get('verdict')}")
        if ab.get("verdict") not in ("normal", "?"):
            print(f"    ⚠ anti-bot triggered — preflight unreliable")
    except Exception:
        pass

    all_failures: list[str] = []
    total_passed = 0
    total_failed = 0

    # ── Page 1: Bookshelf
    await _navigate(ws, "https://draft2digital.com/book/")
    print(f"\n  navigated to: bookshelf")
    p, f, fail = await run_checks(ws, "BOOKSHELF", CHECKS_BOOKSHELF)
    total_passed += p; total_failed += f; all_failures.extend(fail)

    # ── Page 2: Open the first existing book's ebook page to probe.
    # D2D has TWO states per book:
    #   Pre-start: shows only "Start eBook" button + a few inputs (title, volume, BISACs)
    #   Post-start: full wizard with Choose Author, file upload, SAVE & CONTINUE, SUBMIT
    # We auto-detect state and run the appropriate check group.
    print(f"\n  finding first existing book to probe edit form...")
    first_book_url_r = await server.eval_in_tab(ws, """(function() {
        const a = document.querySelector('a.viewbook-link');
        if (!a) return null;
        const href = a.getAttribute('href') || '';
        const m = href.match(/\\/book\\/(?:m\\/)?([0-9]+)/);
        if (m) return 'https://draft2digital.com/book/m/' + m[1] + '/ebook';
        return null;
    })()""")
    book_url = first_book_url_r.get("result", {}).get("value")
    if not book_url:
        print("  ⚠ could not find an existing book to probe edit form — skipping form checks")
    else:
        await _navigate(ws, book_url)
        print(f"  navigated to: {book_url}")
        # Detect state
        state_r = await server.eval_in_tab(ws, "!!document.querySelector('#start-ebook-button')")
        is_pre_start = bool(state_r.get("result", {}).get("value"))
        if is_pre_start:
            print("  state: PRE-START (Start eBook button visible). Running pre-start checks only.")
            print("         Post-start wizard checks (SUBMIT, SAVE & CONTINUE, approval) skipped —")
            print("         those selectors only exist after Start eBook is clicked, which would")
            print("         advance book state (unsafe to do as a probe).")
            p, f, fail = await run_checks(ws, "EBOOK PRE-START", CHECKS_EBOOK_FORM_PRE_START)
        else:
            print("  state: POST-START (full wizard visible). Running full form checks.")
            p, f, fail = await run_checks(ws, "EBOOK WIZARD", CHECKS_EBOOK_FORM_POST_START)
        total_passed += p; total_failed += f; all_failures.extend(fail)

    # ── Summary
    print("\n" + "=" * 60)
    if total_failed == 0:
        print(f"  ✅ ALL GREEN — {total_passed}/{total_passed + total_failed} checks passed")
        print("  D2D recipe is safe to run. No drift since last validation.")
        verdict = "green"
    else:
        print(f"  ⚠️  DRIFT DETECTED — {total_passed}/{total_passed + total_failed} passed, {total_failed} failed")
        print("\nFailures:")
        for line in all_failures:
            print(line)
        print("\n  Do NOT run the production recipe yet — selectors need updating.")
        verdict = "red"
    print("=" * 60)

    status_path = Path.home() / ".webloom" / "logs" / "d2d-preflight-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "verdict": verdict,
        "passed": total_passed,
        "failed": total_failed,
        "failures": all_failures,
        "ts": int(time.time()),
    }, indent=2), encoding="utf-8")
    print(f"\n  status written: {status_path}")

    return 0 if total_failed == 0 else 1


def main():
    p = argparse.ArgumentParser(description="D2D pre-flight check (read-only, no saves)")
    p.add_argument("--port", type=int, required=True, help="Chrome debug port (D2D session)")
    args = p.parse_args()
    sys.exit(asyncio.run(run_preflight(args.port)))


if __name__ == "__main__":
    main()
