"""Probe slot-2 to find current KDP + D2D selectors after the pre-flight detected drift.

Read-only. Navigates Cambrian's KDP pages + an existing D2D book's edit form.
Captures actual current selectors so we can update the pre-flight + Thread.
"""
import asyncio
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    except Exception:
        pass

import server  # noqa: E402

PORT = 9226
BOOK_ID = "AKFYGCKGS9T6Q"


def _list_tabs(port: int):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


async def _ready(ws: str, t: float = 10.0):
    deadline = time.time() + t
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            return
        await asyncio.sleep(0.2)


async def _nav(ws: str, url: str):
    await server.cdp_send(ws, "Page.navigate", {"url": url})
    await _ready(ws)
    await asyncio.sleep(3.0)


async def probe(ws: str, label: str, js: str):
    print(f"\n  ── {label} ──")
    r = await server.eval_in_tab(ws, js)
    val = r.get("result", {}).get("value")
    try:
        print(json.dumps(json.loads(val), indent=2)[:2400])
    except Exception:
        print(str(val)[:2400])


async def main():
    tabs = _list_tabs(PORT)
    ws = next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page")

    # ── KDP DETAILS ────────────────────────────────────────────────────────────
    print("\n========== KDP DETAILS ==========")
    await _nav(ws, f"https://kdp.amazon.com/en_US/title-setup/kindle/{BOOK_ID}/details")

    await probe(ws, "all inputs with name/id containing 'title'", r"""
        JSON.stringify(Array.from(document.querySelectorAll('input, textarea')).filter(el =>
            (el.id||'').toLowerCase().includes('title') ||
            (el.name||'').toLowerCase().includes('title') ||
            (el.placeholder||'').toLowerCase().includes('title')
        ).map(el => ({
            tag: el.tagName, id: el.id, name: el.name, type: el.type,
            placeholder: el.placeholder, visible: el.offsetParent !== null,
            value_present: !!el.value
        })).slice(0, 10))
    """)

    await probe(ws, "AUI runtime presence", r"""
        JSON.stringify({
            has_window_A: !!window.A,
            A_version: window.A?.version || null,
            has_declarative: !!(window.A?.declarative),
            has_declarative_fire: typeof window.A?.declarative?.fire,
            A_top_keys: window.A ? Object.keys(window.A).slice(0, 20) : [],
            has_window_P: !!window.P,
            P_modules_count: window.P?.modules ? Object.keys(window.P.modules).length : 0,
            has_amplify: !!window.amplify,
            scripts_with_amazon: Array.from(document.scripts).map(s => s.src).filter(s => /amazon|aui|aplus/i.test(s)).slice(0, 5),
        })
    """)

    await probe(ws, "Body class + page indicators", r"""
        JSON.stringify({
            body_class: document.body.className.toString().slice(0, 200),
            has_data_action: document.querySelectorAll('[data-action]').length,
            has_a_declarative_class: document.querySelectorAll('.a-declarative').length,
            has_a_button: document.querySelectorAll('.a-button').length,
            framework_markers: {
                next_data: !!window.__NEXT_DATA__,
                nuxt: !!window.__NUXT__,
                react_devtools: !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__,
                redux: !!window.__REDUX_DEVTOOLS_EXTENSION__,
            }
        })
    """)

    # ── KDP CONTENT ────────────────────────────────────────────────────────────
    print("\n========== KDP CONTENT ==========")
    await _nav(ws, f"https://kdp.amazon.com/en_US/title-setup/kindle/{BOOK_ID}/content")

    await probe(ws, "manuscript-source / yes-have-file radios", r"""
        JSON.stringify(Array.from(document.querySelectorAll('input[type=radio]')).filter(el => {
            const ctx = (el.id+' '+el.name+' '+(el.closest('label')?.textContent || '')+' '+(el.getAttribute('data-action')||'')).toLowerCase();
            return /file|content|manuscript|have|own|source/.test(ctx);
        }).slice(0, 15).map(el => ({
            id: el.id, name: el.name, value: el.value,
            label: el.closest('label')?.textContent?.trim().slice(0, 80) || '',
            visible: el.offsetParent !== null
        })))
    """)

    await probe(ws, "AI disclosure / questionnaire section", r"""
        JSON.stringify({
            data_test_ai: Array.from(document.querySelectorAll('[data-test], [data-qa]')).filter(el => /ai|generated|disclosure/i.test(el.getAttribute('data-test')||el.getAttribute('data-qa')||'')).slice(0,5).map(el => ({
                tag: el.tagName, data_test: el.getAttribute('data-test')||el.getAttribute('data-qa'),
                visible: el.offsetParent !== null
            })),
            ids_with_ai: Array.from(document.querySelectorAll('[id*="ai" i], [id*="generated" i]')).slice(0,8).map(el => ({tag: el.tagName, id: el.id, visible: el.offsetParent !== null})),
            text_mentions: (document.body.innerText || '').toLowerCase().includes('artificial intelligence') || (document.body.innerText || '').toLowerCase().includes('ai-generated') || (document.body.innerText || '').toLowerCase().includes('ai content'),
            section_headings_with_ai: Array.from(document.querySelectorAll('h1,h2,h3,h4')).filter(h => /ai|artificial/i.test(h.textContent||'')).slice(0,5).map(h => h.textContent.trim().slice(0,80))
        })
    """)

    # ── KDP PRICING ────────────────────────────────────────────────────────────
    print("\n========== KDP PRICING ==========")
    await _nav(ws, f"https://kdp.amazon.com/en_US/title-setup/kindle/{BOOK_ID}/pricing")

    await probe(ws, "Territory radios + price inputs", r"""
        JSON.stringify({
            territory_radios: Array.from(document.querySelectorAll('input[type=radio]')).filter(el => {
                const ctx = (el.id+' '+el.name+' '+(el.value||'')+' '+(el.closest('label')?.textContent || '')).toLowerCase();
                return /territor|rights|worldwide|individual/.test(ctx);
            }).slice(0,5).map(el => ({id: el.id, name: el.name, value: el.value, label: el.closest('label')?.textContent?.trim().slice(0,80)})),
            price_inputs: Array.from(document.querySelectorAll('input[type=text], input[type=number]')).filter(el => /price|royalt/i.test(el.id+el.name)).slice(0,12).map(el => ({id: el.id, name: el.name, placeholder: el.placeholder, type: el.type})),
            usd_specific: !!document.querySelector('#price-input-usd, [data-test*="price-usd"], input[name*="price"][name*="usd" i]'),
            gbp_specific: !!document.querySelector('#price-input-gbp, [data-test*="price-gbp"], input[name*="price"][name*="gbp" i]')
        })
    """)

    # ── D2D EBOOK FORM ─────────────────────────────────────────────────────────
    print("\n========== D2D EBOOK FORM ==========")
    # Find first existing book to navigate to
    await _nav(ws, "https://draft2digital.com/book/")
    book_url_r = await server.eval_in_tab(ws, """(function(){
        const a = document.querySelector('a.viewbook-link');
        if (!a) return null;
        const href = a.getAttribute('href') || '';
        const m = href.match(/\\/book\\/(?:m\\/)?([0-9]+)/);
        if (m) return 'https://draft2digital.com/book/m/' + m[1] + '/ebook';
        return null;
    })()""")
    bu = book_url_r.get("result", {}).get("value")
    if not bu:
        print("  no existing D2D book found — skipping form probe")
    else:
        await _nav(ws, bu)
        await probe(ws, "All visible button text (find SAVE, SUBMIT replacements)", r"""
            JSON.stringify(Array.from(document.querySelectorAll('button, input[type=submit], a.btn, [class*=btn]')).filter(el => el.offsetParent !== null).slice(0,40).map(el => ({
                tag: el.tagName,
                text: (el.textContent || el.value || '').trim().slice(0, 60),
                id: el.id, classes: (el.className||'').toString().slice(0, 80)
            })))
        """)
        await probe(ws, "Approval checkbox label (was 'I have reviewed this manuscript...')", r"""
            JSON.stringify({
                checkboxes_with_labels: Array.from(document.querySelectorAll('input[type=checkbox]')).slice(0,8).map(cb => ({
                    id: cb.id, name: cb.name,
                    nearest_label: cb.closest('label')?.textContent?.trim().slice(0,200) || document.querySelector('label[for="'+cb.id+'"]')?.textContent?.trim().slice(0,200) || '',
                    visible: cb.offsetParent !== null
                })),
                review_text_on_page: (() => {
                    const t = (document.body.innerText || '').toLowerCase();
                    return {
                        contains_reviewed: t.includes('reviewed'),
                        contains_approve: t.includes('approve'),
                        contains_release: t.includes('release')
                    };
                })()
            })
        """)

    print("\n[probe] done. Use this output to update preflight selectors.")


asyncio.run(main())
