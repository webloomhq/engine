# WebLoom — Canonical Spec

> Last updated: 2026-05-18.
> Single source of truth for what WebLoom is, what we're building, what we're explicitly NOT building, and how the business unfolds. Opinionated. When in doubt, this doc wins over any prior chat thread.

---

## 1. One-line positioning

**WebLoom is open infrastructure for web automation, where the engine is free and the knowledge about how each website works is the marketplace.**

Imagine YouTube but for web-automation knowledge. Anyone who uses a website daily can publish a Thread (a small JSON profile that tells WebLoom how to automate that site) and earn royalties every time another user installs it.

---

## 2. Why now

- MCP ecosystem is 12-18 months old. ~200 production MCPs total, almost all built by companies for their own products.
- 95%+ of the web that humans actually use has no agent-accessible API.
- AI agents are booming (Claude Code, Cursor, Devin, every YC batch) — all hit the same wall: "how do I touch sites that aren't API-accessible?"
- The current alternatives (Playwright, Stagehand, Skyvern, Browserbase) all require dev-level effort per site, expensive LLM-driven exploration, or a clean-room Chrome.
- The window: 12 months ago this would have been too early. 12 months from now Google/Anthropic/OpenAI will have noticed. **Now = perfect first-mover window for a community-driven, Thread-based marketplace.**

---

## 3. Architecture — three tiers

```
┌──────────────────────────────────────────────────────────┐
│  Weaves (workflow bundles)                                │  ← multi-Thread orchestrations, sold as products
├──────────────────────────────────────────────────────────┤
│  Threads (site profile packs)                             │  ← JSON knowledge about ONE site, sold individually
├──────────────────────────────────────────────────────────┤
│  WebLoom Engine                                           │  ← the platform — primitives + learning + execution
└──────────────────────────────────────────────────────────┘
```

### 3.1 The Engine

- Controls real logged-in Chrome via CDP (and eventually via a companion Extension — see §10)
- Ships with 77 tools covering: click, fill, type (3 modes), upload (4 strategies), key events, scroll, scan (ax tree + full), screenshot, navigate, vision Layer 2.5, recipes, network capture, TOTP autofill, profile import/export, stealth flags, React/Redux/AUI/Backbone awareness, touch events, direct XHR upload, playbook learning
- **No real-cursor / pyautogui ever.** Layer 3 permanently disabled. If DOM/CDP/vision fail, hand off to user.
- Stays focused. Site-specific complexity goes into Threads.

### 3.2 Threads — site profile packs

A Thread is a `.thread.json` file. Schema:

```json
{
  "domain": "kdp.amazon.com",
  "name": "KDP Auto-Publish Profile",
  "version": "1.0.0",
  "author": "username",
  "license": "proprietary | mit | cc-by | ...",
  "framework": "amazon-aui",
  "default_strategy": "js",

  "notes": ["AjaxInput clears input.files...", "Modal save needs AUI dispatch..."],

  "click_log": {
    "Choose categories": {"strategy": "cdp", "successes": 12, "failures": 1, "last_at": 1716000000}
  },

  "selectors": {"title_input": "#data-print-book-title", ...},

  "actions": {
    "submit_form": {"type": "aui_dispatch", "event": "a:click", "target": "#submit-button"},
    "upload_cover": {"type": "xhr_upload", "url": "/api/...", "field": "file"}
  },

  "endpoints": {"upload_cover_url": "...", "upload_manuscript_url": "..."},
  "quirks": {...},

  "helpers": {
    "fill_weird_input": "(sel, val) => { /* custom JS */ }"
  }
}
```

The `helpers` field is **the architectural unlock** — Threads ship JS functions that WebLoom evals at runtime. Site-specific complexity lives in the Thread; engine never needs to know about it. Community can solve any site without engine PRs.

Threads are merged into the live playbook at read time:
- Live playbook (user's accumulated learning) wins on conflict
- Threads provide defaults / starter knowledge
- Notes are concatenated, click_log is merged per-entry
- Other keys (default_strategy, framework, quirks, selectors, actions, endpoints, helpers): live entry overrides Thread

### 3.3 Weaves — workflow bundles

A Weave is a `.weave.json` containing:
- Recipe (ordered sequence of WebLoom tool calls)
- Thread dependencies (must be installed)
- Parameter schema (what inputs the user provides)
- Version + price metadata

Example: "Multi-Platform Book Launch Weave" depends on Threads for KDP, D2D, Apple Books, Google Play, Kobo. Takes book metadata as params. Runs the full launch flow across all five platforms in one command.

---

## 4. The product family

| Layer | Name | Description | Price |
|---|---|---|---|
| Engine | **WebLoom** | The MCP, free or near-free | Free (or $9 one-time) |
| Profile packs | **Loom Threads** | Per-site knowledge | $5-30 each |
| Workflow bundles | **Loom Weaves** | Multi-site automations | $50-500 each |
| Subscription | **Loom Pro** | All Threads kept current + API access | $19-49/mo |
| Marketplace | **Loom Atelier** | Where Threads and Weaves are sold | webloom.dev |

---

## 5. Distribution & UX modes

WebLoom supports two browser-control modes:

### 5.1 Debug Chrome mode (current, v0)
- User launches Chrome via `launch_session` — WebLoom spawns it with `--remote-debugging-port` and `--user-data-dir=<their profile>`
- Full CDP power, isTrusted events, all 77 tools work
- Friction: requires closing existing Chrome before launching the debug instance
- Target audience: power users, agents, Thread creators, dev workflows

### 5.2 Extension mode (v0.2 — planned)
- A Chrome Extension lives in the user's NORMAL daily-driver Chrome
- Zero relaunch needed; works on any open tab
- Most WebLoom tools port natively (click, fill, scan, type, react_force_change, redux_dispatch, aui_dispatch, xhr_upload, key events)
- Some tools weaker than CDP (synthetic clicks not isTrusted; file upload via real picker)
- Target audience: everyday users buying Threads to automate daily web

**Final product is hybrid:** Extension as default (low friction). CDP debug as power mode. Most users never know the difference; WebLoom auto-selects per-action.

---

## 6. Marketplace economics

### 6.1 Distribution split

- Engine: free → reach
- Mariano's own Threads (KDP, D2D, etc.): 100% to Mariano
- Community Threads: 70% to author / 30% to WebLoom marketplace
- Weaves: same split if community-authored; 100% if Mariano-authored
- Pro Subscription: 100% to WebLoom (covers infrastructure + premium Threads)
- Sense API (future enterprise tier): 100% to WebLoom

### 6.2 The core value calculation

Without a Thread (engine only):
- Agent explores per site: 10-30 LLM-driven probes
- ~$0.50-3.00 in Claude tokens
- 15-25 min compute time
- Variable reliability

With a Thread installed:
- Engine reads Thread, knows strategy + endpoints + framework
- 1-3 LLM calls (high-level orchestration only)
- ~$0.05-0.30 tokens
- 2-5 min compute time
- High reliability

**Thread break-even = 2-5 uses for a $9 Thread.** As LLMs get more expensive, the value of Threads grows.

### 6.3 Quality controls

Minimum viable launch:
- 5-star ratings + reviews
- Auto-scan badge — WebLoom CI runs each Thread daily against the live site. Green badge if it still works.
- "Verified" tier — Mariano's team manually validates flagship Threads
- Comments per Thread
- Maintainer response time tracker — Threads with active maintainers get a "live" badge
- Forks — anyone can fork an abandoned/broken Thread and republish with attribution

Later additions:
- Telemetry-driven freshness signals
- Cross-Thread queries ("show me every Thread for AUI sites")
- Version compatibility tags

---

## 7. Engine growth mechanism

The engine grows signal-driven, NOT speculatively.

| Signal | What it tells us |
|---|---|
| 5+ Threads ship the same `eval_js` workaround | Promote that workaround to a first-class engine tool |
| Thread-creator dropout (started → abandoned) | A primitive is blocking |
| Aggregated click_log failures on the same DOM pattern | Existing strategies don't cover this pattern |
| Direct user requests | Highest-quality but lowest-volume signal |
| Quarterly review of top-50 Thread `helpers` blobs | Find common patterns to promote |
| Dogfooding (Mariano's daily use) | Real walls surface real gaps |

**Rule: We don't add engine tools speculatively. We add them when patterns emerge from real Thread creation.**

What we explicitly do NOT add:
- Built-in CAPTCHA solver (separate paid integration only)
- Cloud browsers (Browserbase territory)
- Identity provisioning (separate `burner-mcp`)
- OS-level automation (`computer-mcp`)
- Stealth at Verified-Sessions level (different category)
- Vector store / RAG (different MCP)

---

## 8. Engine capability snapshot (current, v0)

Tools shipped tonight:
- Click: actionability + CDP isTrusted → JS dispatch (pointer+mouse+click on leaf, AUI wrapper-aware) → vision (Claude)
- Upload: Strategy A (file-chooser intercept), B (CDP setFileInputFiles via objectId), C (synthetic drop), D (DataTransfer.files inject), `xhr_upload` (direct fetch with credentials)
- Type: keystrokes (per-char keyDown+keyUp with full metadata), insertText, fast
- Key press: Enter/Tab/Esc/Arrows/etc. with modifiers
- Touch: `touch_tap` via CDP `Input.dispatchTouchEvent` for mobile-first webapps
- React: `react_force_change` (fiber walk + onChange invoke)
- Redux: `react_inspect_store` (4-tier discovery), `redux_dispatch`
- Amazon AUI: `aui_dispatch` (inspect + fire `A.declarative.fire`)
- Backbone: `backbone_inspect`
- Scan: `scan_tab` (ax mode with @eN refs by default, or full mode), `scan_tab_diff`
- Vision: Layer 2.5 via Claude Sonnet; Florence-2 parked
- Auth: `auth_totp` (pyotp), `pause_for_human`
- Profile: `export_profile`, `import_profile`
- Network: `capture_network_start/stop`, `get_captured_requests`
- Recipes: `start_recording`, `end_recording`, `list_recipes`, `replay_recipe` with `{{var}}` substitution
- Playbook: `get_playbook`, `save_playbook`, `note`, plus active strategy selection in click handler
- Threads: `list_threads`, `install_thread`, `export_thread`

CDP layer: persistent pool, monotonic IDs, id-keyed response futures, subscriber API for streaming events, keepalive pings, retry-on-disconnect.

Smoke test: 23/23 passing.

---

## 9. Roadmap

### Phase 1 — Launch readiness (next 1-3 sessions)

| Item | Status | Notes |
|---|---|---|
| Engine: replay_xhr | Pending | Replay a captured Network request — unlocks auto-discovery of upload endpoints, cracks AjaxInput class |
| Engine: aui_inspect_state | Pending | Walk window.A's hidden state container — cracks KDP modal saves |
| Engine: wait_for_any | Pending | Multi-condition timing |
| Engine: Thread `helpers` evaluator | Pending | The architectural unlock for community-driven complexity |
| Engine: seed_thread_generator.py | Pending | Headless crawl that emits Seed Threads for popular sites |
| Initial Thread library | In progress | KDP and D2D Threads exported tonight; ~100-200 Seed Threads via generator next |
| webloom.dev landing page | Pending | Marketplace + docs |
| webloom.dev/atelier listings for Pro Threads | Pending | KDP + D2D as flagship $19 listings |
| Discord/community space | Pending | Where Thread creators talk |

### Phase 2 — Extension companion (next 2-3 weeks)

- Chrome Extension build (Manifest V3)
- Native Messaging bridge to WebLoom MCP server
- Chrome Web Store submission (positioned as "AI agent web companion / workflow automation")
- Sideload fallback at webloom.dev if Web Store delays
- Auto mode (extension first, CDP fallback for sites needing isTrusted)

### Phase 3 — Community and quality (next 1-2 months)

- Auto-scan CI for installed Threads
- Ratings + reviews + forks
- Verified tier
- Creator dashboard (royalties, install counts, Thread health)
- Subscription tier (Loom Pro)

### Phase 4 — Adjacent products (3-6 months)

- `burner-mcp` for identity provisioning (catch-all domain, SimpleLogin, SMS-Activate, proxy rotation)
- `replay_xhr` becomes a public Sense API for site-pattern queries
- Cross-Thread queries ("show me every site whose framework is X")
- Built-with-WebLoom certified tier
- Workflow marketplace (Weaves)

---

## 10. Explicitly out of scope

These are NOT WebLoom's job:

- ❌ CAPTCHA-solving as a built-in service (offer as paid integration only)
- ❌ Cloud-hosted browser farms (Browserbase / partner integration if needed)
- ❌ Identity provisioning / burner accounts (separate `burner-mcp`)
- ❌ OS-level desktop automation (`computer-mcp`)
- ❌ Anti-bot stealth at the Verified-Sessions tier (specialized vendors)
- ❌ Vector DB / RAG infrastructure
- ❌ Workflow scheduling / cron (use Hive or OS tools)
- ❌ Promoting Threads explicitly for ToS-violating use cases (knowledge sharing is legal; we don't market spam workflows)

---

## 11. Risk model

| Risk | Likelihood | Mitigation |
|---|---|---|
| Chrome Web Store rejects extension | Medium (30-40%) | Iterate on listing language; sideload fallback always available |
| Marketplace reputation suffers from "shady" Threads | Medium | Keep marketplace at webloom.dev; extension at Web Store as legitimate tool. Don't market for spam. |
| Site changes break N Threads at once | Inevitable | Auto-scan CI surfaces breakage daily; maintainers get notified |
| LLM costs go up dramatically | Possible | Increases Thread value (Threads save tokens) — actually a tailwind |
| Anthropic/Google/OpenAI builds competing offering | Likely 12-24 months out | First-mover + community marketplace = data moat that's hard to replicate |
| Marketplace cold-start fails | Medium | Mitigated by Seed Thread generator (launch with 100+ Threads on day 1) |
| Single popular site Thread becomes monopoly target for competing forks | Possible | Allow multiple Threads per site for different workflows / audiences; ratings sort it out |

---

## 12. The community angle (core moat)

Three reasons WebLoom is community-first, not B2B-first:

1. **Monetization-by-participation is the stickiest feature.** Users stay because they earn. YouTube, Substack, Etsy proved this.
2. **The long tail of the web is undefeatable for any single team.** Million+ sites. Only a community model can cover it.
3. **AI agents are exploding; everyone needs the same Threads.** When your KDP Thread works for Mariano, Carlos, and 10,000 strangers' agents, network effects compound.

---

## 13. The reflexive loop (the strategic vision)

```
        ┌─────────────────────────────┐
        │      webloom.dev            │
        │     (marketplace)           │
        └──────────────┬──────────────┘
                       │
        publishes  ┌───┴───┐  downloads
                   │       │
                   ▼       ▼
              ┌────────────────┐
              │  User population │
              │  (anyone with    │
              │  Chrome + agent) │
              └────────────────┘
                       │
                       │ uses Threads on the web
                       ▼
              ┌────────────────┐
              │   The Web       │
              │ (every site)    │
              └────────────────┘
                       │
                       │ learns new patterns
                       ▼
              ┌────────────────┐
              │  New Threads    │
              │  authored       │
              └────────────────┘
                       │
                       └── back to webloom.dev (publish → earn)
```

WebLoom indirectly maps the entire web through accumulated Thread metadata: which sites use what framework, which have what quirks, which need what tools. **You'd accumulate the most detailed survey of "how the production web actually works" that exists outside Google.** That's the moat.

---

## 14. Tonight's accomplishments (snapshot for future me)

- Renamed engine: chrome-mcp → WebLoom (server identity, .mcp.json keys, directory, registry)
- Added pluggable Thread architecture: loader, merge logic, install/export/list tools
- Path migration: `~/.chrome-mcp/` → `~/.webloom/` with auto-fallback for legacy
- Exported first 2 production Threads: KDP and D2D
- Added 6 tools tonight: aui_dispatch, backbone_inspect, xhr_upload, touch_tap, react_force_change (early), react_inspect_store, redux_dispatch, key_type modes, key_press
- Click handler rewritten around actionability loop with playbook-informed strategy selection
- Upload now has 4 strategies (A/B/C/D)
- Vision Layer 2.5 with scroll-into-view + Claude backend (Florence parked)
- Smoke test: 23/23 passing in isolated headless Chrome
- WEBLOOM.md architecture doc
- Layer 3 / real-cursor permanently disabled

---

## 15. Decisions locked in (don't re-litigate)

- **Engine name: WebLoom.** Technical key in `.mcp.json`: `webloom`.
- **Marketplace domain: webloom.dev (or webloom.ai if .dev taken).** Off Chrome Web Store; our jurisdiction.
- **Three-tier product: Engine + Threads + Weaves.** Pricing free-engine / paid-content.
- **No Layer 3 / real cursor, ever.** Permanently disabled.
- **Engine stays lean.** Thread `helpers` carry site-specific JS. New primitives only when 5+ Threads need same workaround.
- **Community-first marketing.** Threads can be authored and sold by anyone. Marketplace is the moat.
- **Distribution: Extension (default) + CDP debug (power mode).** Built in that order.
- **Burner-mcp is separate.** Identity provisioning is not WebLoom's job.

---

## 16. Next concrete builds (this session continuing)

**Track A — Seed Thread generator via sandbox** (in progress)
- Headless Chrome crawl that visits N popular sites and emits starter Threads
- Outputs framework detection, default strategy, visible selectors, common quirks
- Marketplace launches with 100-200 Threads
- Quality tier: "Starter" (free or $1-2) vs Pro (hand-crafted, $10-30)

**Track B — Engine upgrade via sandbox testing**
- Build `replay_xhr` (next critical primitive)
- Test against synthetic + real sites in headless
- Update smoke test
- Pattern: every new engine tool ships with sandbox tests

**Track C — Thread helpers evaluator** (architectural unlock)
- Make WebLoom eval `thread.helpers[...]` JS at runtime
- Add `call_helper` action type
- Document in WEBLOOM.md

Pace: build incrementally, smoke-test after each, run real-site sanity check via headless Chrome, never touch user's daily-driver Chrome.

---

*End of spec. When in doubt, this doc wins.*
