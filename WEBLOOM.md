# WebLoom

Autonomous browser engine for AI agents. Three layers:

```
┌──────────────────────────────────────────────────────────┐
│  Weaves (workflow bundles — recipes + thread deps)       │  ← sold as products
├──────────────────────────────────────────────────────────┤
│  Threads (site profile packs — JSON knowledge)           │  ← sold individually
├──────────────────────────────────────────────────────────┤
│  WebLoom (engine — this MCP)                              │  ← the platform
└──────────────────────────────────────────────────────────┘
```

## What WebLoom (the engine) does

- Controls real logged-in Chrome via CDP — preserves your sessions, cookies, fingerprint
- DOM/CDP click ladder with actionability waiting + verifier (Playwright-class)
- 4-strategy file upload (Strategy A/B/C/D — beats Playwright on D2D/KDP-style label-wrapped inputs)
- React-aware typing + fiber-walk onChange invoker
- Redux store discovery + dispatch
- Amazon AUI declarative event invocation
- Backbone.js introspection
- Synthetic touch events for mobile-first webapps (Telegram Web /a/)
- Direct XHR upload with cookie/CSRF for hostile AjaxInput widgets
- Network capture + replay
- Stealth launch flags
- Vision Layer 2.5 (Claude vision grounding for canvas / Shadow DOM)
- TOTP autofill (pyotp)
- Profile import/export (cookies)
- Recipe record/replay with parameter substitution
- Playbook learning — per-domain, per-element success rate tracking
- @eN accessibility-tree refs (compressed scan)

## What Threads are

A Thread is a `.thread.json` file containing accumulated knowledge about ONE site. It ships separately from the engine. When WebLoom reads its playbook for a domain, Threads are merged in (Thread data + live learning, with live winning on conflicts).

### Thread schema

```json
{
  "domain": "kdp.amazon.com",
  "name": "KDP Profile",
  "version": "1.0.0",
  "author": "Mariano @ VibeDNA",
  "license": "proprietary",
  "framework": "amazon-aui",

  "default_strategy": "js",

  "notes": [
    "AjaxInput clears input.files after onchange — use xhr_upload",
    "AUI submit needs A.$().trigger('click')",
    "Categories button hidden behind a-page overlay on second open"
  ],

  "click_log": {
    "Choose categories": {"strategy": "cdp", "successes": 12, "failures": 1, "last_at": 1716000000}
  },

  "selectors": {
    "title_input": "#data-print-book-title",
    "categories_button": "#categories-modal-button",
    "save_categories": "..."
  },

  "actions": {
    "submit_form": {
      "type": "aui_dispatch",
      "event": "a:click",
      "target": "#submit-button-announce"
    },
    "upload_cover": {
      "type": "xhr_upload",
      "url": "/api/title-management/cover/upload",
      "field": "file",
      "csrf_field": "anti-csrftoken-a2z"
    }
  },

  "quirks": [
    "form-submit-blacklist for paperback workflow",
    "Accordion sections must be opened via A.$().trigger('click') before fill works"
  ]
}
```

Standard locations WebLoom checks for Threads:

```
<engine>/threads/*.thread.json       ← bundled defaults (ship with WebLoom)
~/.webloom/threads/*.thread.json     ← user-installed
```

## Tools

| Tool | Purpose |
|---|---|
| `list_threads` | Show installed Threads |
| `install_thread(path)` | Drop a `.thread.json` into `~/.webloom/threads/` |
| `export_thread(domain, ...)` | Export your live learning for a domain as a portable Thread |
| `get_playbook(domain)` | Read merged (live + Threads) knowledge |
| `save_playbook(domain, key, value)` | Write to live playbook (Thread data not mutated) |

## How a user gets WebLoom

### What's in the box

**Engine install** (one of):
- MCP marketplace install — adds `webloom` to their `.mcp.json` (engine-only)
- Direct download — Python package + standalone server

**Folder created on install**:
```
~/.webloom/
  ├── threads/              ← Threads they install
  ├── playbook.json         ← live learning (auto-built as they use it)
  └── sessions.json         ← session configs (port, profile dirs)
```

### Buying flow for Threads / Weaves

1. User installs WebLoom (free or low-cost)
2. They try to automate KDP → engine works but discovers every quirk live (slow + occasional failures)
3. They buy "KDP Thread" ($9 on Gumroad) — downloads `kdp.amazon.com.thread.json`
4. They run `install_thread(path="~/Downloads/kdp.amazon.com.thread.json")`
5. Next KDP session: WebLoom auto-uses your battle-tested default strategy, knows the upload endpoint, knows the AUI quirks, etc. → 10x faster, no exploration needed

For Weaves (workflow bundles), same flow but bundle ships as a `.weave.json` containing recipe + Thread dependencies + parameter schema.

## Updating

Threads/Weaves are versioned. When a site changes:
- Author republishes the Thread with bumped version
- Subscribers get an update (push from marketplace, or pull via `webloom update`)
- Live playbook keeps user-specific learnings; only the Thread layer changes
