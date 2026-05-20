# WebLoom

**Browser automation MCP for AI agents. Engine for the [WebLoom Atelier](https://webloom.run) marketplace.**

WebLoom is a Model Context Protocol server that gives any MCP-aware AI client (Claude Code, Cursor, Continue, Claude Desktop) 62 browser-control tools — click, fill, upload, screenshot, AUI dispatch, redux dispatch, Lexical text setting, file injection (5 strategies), XHR replay, network capture, recipe record/replay, and a learning playbook that records every successful action so the next run skips the trial-and-error phase.

The engine is free. Knowledge about each website lives in **Threads** — small JSON profile packs authored by humans who already use those sites daily. Buy them, sell them, fork them at [webloom.run](https://webloom.run).

## Quick install

Tell your AI client (Claude Code, Cursor, etc.):

```
Install WebLoom MCP for me.

Steps:
1. Clone: git clone https://github.com/webloomhq/engine ~/.webloom/engine
2. Install deps: cd ~/.webloom/engine && pip install -r requirements.txt
3. Create playbook dirs: mkdir -p ~/.webloom/threads ~/.webloom/logs
4. Add to my MCP config (~/.mcp.json or equivalent):

{
  "mcpServers": {
    "webloom": {
      "type": "stdio",
      "command": "python",
      "args": ["~/.webloom/engine/server.py"]
    }
  }
}

5. Tell me to restart the client.
```

After restart, you have 62 browser-control tools and a local playbook at `~/.webloom/` that fills automatically as you drive websites.

## What goes where

```
~/.webloom/
├── engine/             ← this repo
├── threads/            ← *.thread.json files (installed or authored)
├── playbook.json       ← your accumulated learning per domain
└── logs/
```

## Why a Thread

A Thread captures everything that makes one specific website work:

- **Pre-flight selectors** — what to check is still present before you run a recipe
- **Proven actions** — descriptors + click strategies + selectors + wait timings that succeeded on a real run
- **Field schemas** — what type of value goes in each input
- **Click strategies per descriptor** — `aui_dispatch` for Amazon, `react_root_direct` for D2D, `lexical-api` for Reddit, `strategy_e` (XHR observer + real-picker metadata) for KDP manuscript upload
- **Coverage gaps** — bugs we're still cracking, publicly tracked

When your agent calls `click(description="Save categories")` on a site with an installed Thread, the playbook says "use AUI dispatch for this button" and the click works first try. Without a Thread, the agent has to figure that out itself, often after 5-20 minutes of failed clicks.

## The five upload strategies

| Strategy | When |
|---|---|
| **A** — file-chooser intercept | Visible trigger opens a real OS picker |
| **B** — Runtime.evaluate objectId | Shadow DOM / hidden inputs / iframes |
| **C** — synthetic drag-drop | React-controlled drop targets |
| **D** — DataTransfer.files inject | Label-wrapped hidden inputs (D2D, KDP cover) |
| **E** — AjaxInput + XHR observer | KDP manuscript, Vendor Central, A+ Content (auto-detects via `id$="-AjaxInput"` or `.fileuploader` parent) |

## The click ladder

1. **AUI declarative fire** — Amazon AUI buttons get `A.declarative.fire(action, host, event)`
2. **CDP isTrusted click** — coords resolved via actionability probe + Input.dispatchMouseEvent
3. **JS dispatch sequence** — pointerdown + mousedown + pointerup + mouseup + click on leaf, then bubble
4. **Vision grounding** — Claude finds the element in a screenshot when DOM resolution fails

No real OS cursor. Ever.

## State commit primitives

Some flows close a modal cleanly but the form state doesn't persist — modal save handlers read from Redux, Context, or Backbone, not from React fiber. WebLoom has six store-discovery paths:

1. Cached store from previous call
2. Global `window.store` / `window.__store__` / `window.__REDUX_STORE__`
3. Fiber walk from selector ancestor
4. Full React DevTools tree walk
5. Direct React root discovery (no devtools hook needed — finds `__reactContainer*` / `_reactRootContainer` keys)
6. Backbone bridge (wraps Backbone Model `.get/.set/.save` as dispatch shape)

Pair with `redux_dispatch({type: "SET", payload: {...}})` to commit state that clicking alone can't reach.

## Status

| Layer | State |
|---|---|
| Engine (62 tools) | ✅ Stable. MIT licensed. |
| Playbook (22 record sites) | ✅ Closing loop on every click, fill, key_type, upload, dispatch, navigate, screenshot, scroll, wait_for. |
| Threads | 🌱 Early — 90+ pre-mapped at [webloom.run/claim](https://webloom.run/claim). Founder collection: KDP, D2D, Upwork. |
| Atelier marketplace | 🌱 In private beta. |

## License

MIT — see [LICENSE](./LICENSE).

## Contributing

Thread contributions: PR a `*.thread.json` file under `threads/` (separate repo, coming soon). Engine contributions: PR here. Authors of accepted Threads earn royalties on every install — see [webloom.run/authors](https://webloom.run/authors).
