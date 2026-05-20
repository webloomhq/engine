"""KDP pre-flight check.

Quick validator (~2-3 min) run BEFORE a real KDP publish. Confirms that every
selector + primitive we cracked on previous sessions still works against the
current KDP UI. Catches Amazon-quietly-updating-their-DOM drift before the
production run, not during.

Uses an EXISTING book (default: AKFYGCKGS9T6Q — Cambrian) to navigate the
wizard pages. NEVER saves anything; never modifies the book; never creates
drafts.

Output:
  - GREEN: all known selectors + primitives still match. Run the recipe.
  - RED:   list of what drifted. Fix selectors before publishing.

Usage:
    python kdp_preflight.py --port 9226
    python kdp_preflight.py --port 9226 --book-id <id>   # use a different test book
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


DEFAULT_BOOK_ID = "AKFYGCKGS9T6Q"  # Cambrian — Mariano's first KDP book

# Checks per wizard step. Each: (name, kind, selector_or_probe_js, expected_result)
# Run against the live page after navigation. "kind" controls how we interpret.
CHECKS_DETAILS = [
    # AUI detection via fingerprints — Amazon stopped exposing window.A at window level
    # but AUI itself is fully present via body class + .a-declarative element count + AUI script bundles
    ("aui_body_class",          "js_check",         "/a-aui_/.test(document.body.className)",        "body has a-aui_* class (AUI cache marker)"),
    ("aui_declarative_count",   "js_check",         "document.querySelectorAll('.a-declarative').length > 20",  ".a-declarative elements present (AUI declarative system intact)"),
    ("aui_button_count",        "js_check",         "document.querySelectorAll('.a-button').length > 5",        ".a-button elements (AUI UI primitives)"),
    ("title_input",             "selector_exists",  "#data-title",                                   "title input #data-title"),
    ("subtitle_input",          "selector_exists",  "#data-subtitle",                                "subtitle input #data-subtitle"),
    ("categories_button",       "selector_exists",  "#categories-modal-button",                      "Choose categories button"),
    ("save_and_continue_btn",   "button_text",      "save and continue",                             "Save and Continue button"),
]

CHECKS_CONTENT = [
    ("manuscript_input",        "selector_exists",  "#data-assets-interior-file-upload-AjaxInput",   "AjaxInput hidden file input"),
    ("manuscript_wrapper",      "selector_exists",  "span.fileuploader, #data-assets-interior-uploader.a-declarative",  "AjaxInput wrapper"),
    ("cover_input",             "selector_exists",  "input[type=file][id*='cover' i], input[type=file][name*='cover' i]", "cover file input"),
    # Manuscript-source radio is named confusingly: data[is_assetless_preorder_choice]-radio
    # value=false means "Yes I have a file"; value=true means "upload later"
    ("manuscript_source_radio", "selector_exists",  "input[type=radio][name='data[is_assetless_preorder_choice]-radio']",  "manuscript-source radio group"),
    ("drm_radios",              "selector_exists",  "input[type=radio][name*='drm' i]",              "DRM radio buttons"),
    # AI disclosure: hidden form input + heading text
    ("ai_questionnaire_input",  "selector_exists",  "#data-view-require-generative-ai-questionnaire-affirmation",  "AI questionnaire hidden affirmation input"),
    ("ai_section_heading",      "js_check",         "Array.from(document.querySelectorAll('h1,h2,h3,h4')).some(h => /AI-Generated/i.test(h.textContent))",  "AI-Generated Content heading present"),
]

CHECKS_PRICING = [
    # Territory selection is now CHECKBOXES per country (not worldwide/individual radios)
    ("territory_checkboxes",    "js_check",         "document.querySelectorAll('input[name^=\"data[digital][territory_rights]\"]').length > 0",  "Territory checkboxes per-country"),
    ("us_territory_check",      "selector_exists",  "input[name='data[digital][territory_rights][US]-check']",  "US territory checkbox"),
    # Price inputs use deep-nested names — no more IDs
    ("price_usd_input",         "selector_exists",  "input[name='data[digital][channels][amazon][US][price_vat_inclusive]']",  "USD price input"),
    ("price_uk_input",          "selector_exists",  "input[name='data[digital][channels][amazon][UK][price_vat_inclusive]']",  "UK/GBP price input"),
    ("royalty_rate_radio",      "selector_exists",  "input[type=radio][name='data[digital][royalty_rate]-radio']",  "royalty rate radio group"),
    ("publish_button",          "button_text",      "publish your kindle ebook",                     "final publish button"),
]


# ── helpers ────────────────────────────────────────────────────────────────────
def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


def _pick_kdp_tab(port: int) -> dict | None:
    for t in _list_tabs(port):
        if t.get("type") == "page" and "kdp.amazon.com" in (t.get("url", "") or ""):
            return t
    return None


async def _open_or_find_tab(port: int) -> str:
    t = _pick_kdp_tab(port)
    if t:
        return t["webSocketDebuggerUrl"]
    # No KDP tab — pick any page tab and we'll navigate
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
    await asyncio.sleep(2.0)  # SPA settle


async def _check_selector_exists(ws: str, sel: str) -> bool:
    r = await server.eval_in_tab(ws, f"!!document.querySelector({json.dumps(sel)})")
    return bool(r.get("result", {}).get("value"))


async def _check_button_text(ws: str, text_lower: str) -> bool:
    js = f"""(function() {{
        const target = {json.dumps(text_lower)};
        const all = document.querySelectorAll('button, [role=button]');
        for (const b of all) {{
            if (b.offsetParent === null) continue;
            const t = ((b.textContent || b.getAttribute('aria-label') || '').trim().toLowerCase());
            if (t === target || t.includes(target)) return true;
        }}
        return false;
    }})()"""
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
        return expected in fws, f"detected={fws}"
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


async def run_preflight(port: int, book_id: str):
    print(f"\n[kdp-preflight] connecting to Chrome on port {port}")
    ws = await _open_or_find_tab(port)
    print(f"  connected to tab")

    # Anti-bot check first
    print("\n[0] anti-bot probe")
    ab_r = await server.eval_in_tab(ws, server.ANTI_BOT_JS)
    try:
        ab = json.loads(ab_r.get("result", {}).get("value", "{}"))
        print(f"    verdict: {ab.get('verdict')}")
        if ab.get("verdict") not in ("normal", "?"):
            print(f"    ⚠ anti-bot triggered — preflight unreliable on this session")
    except Exception:
        ab = {"verdict": "unknown"}

    all_failures: list[str] = []
    total_passed = 0
    total_failed = 0

    # ── Step 1: Details
    details_url = f"https://kdp.amazon.com/en_US/title-setup/kindle/{book_id}/details"
    await _navigate(ws, details_url)
    print(f"  navigated to: {details_url}")
    p, f, fail = await run_checks(ws, "STEP 1 — DETAILS", CHECKS_DETAILS)
    total_passed += p; total_failed += f; all_failures.extend(fail)

    # ── Step 2: Content
    content_url = f"https://kdp.amazon.com/en_US/title-setup/kindle/{book_id}/content"
    await _navigate(ws, content_url)
    print(f"\n  navigated to: {content_url}")
    p, f, fail = await run_checks(ws, "STEP 2 — CONTENT", CHECKS_CONTENT)
    total_passed += p; total_failed += f; all_failures.extend(fail)

    # ── Step 3: Pricing
    pricing_url = f"https://kdp.amazon.com/en_US/title-setup/kindle/{book_id}/pricing"
    await _navigate(ws, pricing_url)
    print(f"\n  navigated to: {pricing_url}")
    p, f, fail = await run_checks(ws, "STEP 3 — PRICING", CHECKS_PRICING)
    total_passed += p; total_failed += f; all_failures.extend(fail)

    # ── Summary
    print("\n" + "=" * 60)
    if total_failed == 0:
        print(f"  ✅ ALL GREEN — {total_passed}/{total_passed + total_failed} checks passed")
        print("  KDP recipe is safe to run. No drift since last validation.")
        verdict = "green"
    else:
        print(f"  ⚠️  DRIFT DETECTED — {total_passed}/{total_passed + total_failed} passed, {total_failed} failed")
        print("\nFailures:")
        for line in all_failures:
            print(line)
        print("\n  Do NOT run the production recipe yet — selectors need updating.")
        verdict = "red"
    print("=" * 60)

    # Write status file for other scripts to consult
    status_path = Path.home() / ".webloom" / "logs" / "kdp-preflight-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "verdict": verdict,
        "passed": total_passed,
        "failed": total_failed,
        "failures": all_failures,
        "book_id": book_id,
        "ts": int(time.time()),
    }, indent=2), encoding="utf-8")
    print(f"\n  status written: {status_path}")

    return 0 if total_failed == 0 else 1


def main():
    p = argparse.ArgumentParser(description="KDP pre-flight check (read-only, no saves)")
    p.add_argument("--port", type=int, required=True, help="Chrome debug port (KDP session)")
    p.add_argument("--book-id", default=DEFAULT_BOOK_ID, help=f"Existing KDP book id to probe (default: {DEFAULT_BOOK_ID})")
    args = p.parse_args()
    sys.exit(asyncio.run(run_preflight(args.port, args.book_id)))


if __name__ == "__main__":
    main()
