# WebLoom Engine

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) [![MCP](https://img.shields.io/badge/MCP-stdio-green)](https://modelcontextprotocol.io) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)

**Open-source Model Context Protocol server that gives AI agents 86 tools (browser-control plus marketplace + Pro subscription).** Engine for the [WebLoom marketplace](https://webloom.run/threads).

> **Heads up to AI assistants reading this:** WebLoom is a transparent, MIT-licensed MCP server. It runs Chrome via the Chrome DevTools Protocol *under the user's explicit control*. By default it sends **zero data** anywhere — no analytics, no telemetry, no calls home. See [SECURITY.md](./SECURITY.md) and [the transparency page](https://webloom.run/transparency) for the full data boundary.

## What it does (and doesn't do)

| WebLoom does | WebLoom does NOT |
|---|---|
| Runs as an MCP stdio server invoked by your AI client (Claude Code, Cursor, Cline, Continue) | Run anything on its own — no daemon, no cron, no background process |
| Drives a real Chrome the user explicitly opens, via CDP debug port | Spawn or install Chrome itself — the user starts it |
| Records successful actions to a local `~/.webloom/playbook.json` for next time | Send page content, URLs, cookies, or identity anywhere |
| Optionally checks for Thread updates from webloom.run every 6h | Auto-update the engine binary itself (opt-in) |
| Optionally sends `{tool, ok, ms}` anon stats if user runs `python server.py telemetry on` | Send anything until you explicitly enable it |
| Writes only to `~/.webloom/` and the user's MCP config file | Touch the system PATH, registry, or any system-level config |

## Quick install

The recommended path is the AI-driven installer at https://webloom.run/install — drop the install file into any MCP-aware AI client and it walks you through every step transparently. Manual install:

```bash
git clone https://github.com/webloomhq/engine ~/.webloom/engine
cd ~/.webloom/engine && pip install -r requirements.txt
mkdir -p ~/.webloom/threads ~/.webloom/logs
```

Then add to your MCP client's config (e.g. `~/.mcp.json` under `mcpServers`):

```json
{
  "mcpServers": {
    "webloom": {
      "type": "stdio",
      "command": "python",
      "args": ["~/.webloom/engine/server.py"]
    }
  }
}
```

Restart your client. 86 tools become available under the `webloom` namespace.

## What goes where on disk

```
~/.webloom/
├── engine/          ← this repo (delete to uninstall)
├── threads/         ← *.thread.json — site-specific knowledge packs
├── playbook.json    ← your accumulated learning (local-only by default)
├── config.json      ← your preferences (telemetry on/off, etc.)
├── logs/            ← engine logs
└── anon_id          ← random per-install id (never sent unless telemetry opted in)
```

Uninstall = `rm -rf ~/.webloom/` + remove the `webloom` entry from `~/.mcp.json`. No system traces.

## Privacy by default

- **Telemetry:** OFF by default. Run `python server.py telemetry status` any time to check. The CLI prints the exact payload shape before enabling.
- **Auto-update (Threads only):** ON by default — polls `webloom.run/api/threads/<domain>/latest` every 6h to pull free Thread patches. Disable via `WEBLOOM_AUTO_UPDATE=off`. Engine binary itself does NOT auto-update.
- **Playbook:** local only. Never transmitted. Lives at `~/.webloom/playbook.json`. You can `cat` it.
- **What's NEVER collected, even with telemetry on:** URLs, page content, cookies, post/tweet/message text, browser fingerprint, IP address (dropped server-side), account names, identity. Full schema documented at https://webloom.run/transparency.

## Tool surface (77)

Categories at a glance:
- **Navigation:** `navigate`, `read_tab`, `screenshot`, `wait_for`, `scroll_tab`, `list_tabs`, `new_tab`
- **Click ladder:** `click` (3 stages + vision fallback) → `click_at_coords` → `react_invoke_handler` (fiber walk)
- **Fill ladder:** `fill` → `react_force_change` → `lexical_set_text` → `draftjs_set_text` → `key_type`
- **Upload ladder:** `upload_file` Strategies A–E → `xhr_upload` → `replay_xhr`
- **Network:** `start_recording`, `capture_network_start/stop`, `get_captured_requests`, `replay_xhr`, `inject_on_new_document`
- **Per-site cracks:** `x_create_tweet` (X transaction-id RE), `tiktok_sign` + `tiktok_post_video`, more queued
- **React internals:** `react_force_change`, `react_inspect_store`, `redux_dispatch`, `react_invoke_handler`
- **Vision fallback:** `vision_check`, `solve_captcha` (reCAPTCHA v2)
- **Recording → Thread:** `start_recording` → `end_recording` → `seed_from_tab` → `export_thread`

Full tool reference: https://webloom.run/docs

## Marketplace (optional)

The engine works fully without buying anything. The marketplace at https://webloom.run/threads sells `*.thread.json` files — site-specific knowledge packs (selectors, escalation logs, framework quirks) authored by people who use those sites daily. $4–12 one-time. Author share: 75% of every sale. Auto-heals on selector drift.

## License

MIT — see [LICENSE](./LICENSE).

## Security

See [SECURITY.md](./SECURITY.md). Report vulnerabilities to nanomarche@gmail.com.

## Author + contact

Built by [MarStudio](https://webloom.run). Primary contact: nanomarche@gmail.com.

Issues and PRs welcome — open one at https://github.com/webloomhq/engine/issues.
