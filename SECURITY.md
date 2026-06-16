# Security Policy

## Reporting a Vulnerability

If you discover a security issue in WebLoom, please email **nanomarche@gmail.com** with details. We respond within 72 hours.

Do NOT open a public GitHub issue for security reports — open one only after the fix has shipped.

## What WebLoom does on your machine

WebLoom runs as an **MCP stdio server**. It is invoked by your AI client (Claude Code, Cursor, Cline, Continue, Claude Desktop) when you ask the AI to do something browser-related. It exits when the client exits.

It runs Chrome via the **Chrome DevTools Protocol** — the same protocol Puppeteer, Playwright, and the official VS Code debugger use. Chrome must be started by the user with `--remote-debugging-port=<n>`. WebLoom attaches to that port; it does not spawn Chrome on its own.

## What WebLoom NEVER does

- Collects, transmits, or stores **page content** — no DOM dumps, no scraped data, no screenshots sent off-machine
- Collects URLs you visit (your own privacy boundary — `~/.webloom/playbook.json` is local-only and never leaves your disk by default)
- Reads or transmits cookies, session tokens, CSRF tokens, or any authentication material — those stay in your Chrome session, where they belong
- Records keystrokes outside the Chrome tab you're driving
- Touches the system PATH, registry, daemons, cron, or any system-level configuration
- Auto-update the engine binary without your action — only Thread files (community-authored site knowledge) are updated automatically, and that's opt-out via `WEBLOOM_AUTO_UPDATE=off`
- Talk to any third-party analytics, ad networks, or tracking SDKs

## What WebLoom MAY collect (opt-in only, OFF by default)

If — and only if — the user explicitly runs `python server.py telemetry on`, the engine sends one tiny payload after each tool call:

```json
{
  "tool":           "x_create_tweet",
  "ok":             true,
  "error_class":    null,
  "duration_ms":    412,
  "engine_version": "0.3.0",
  "anon_id":        "a8f3c2d1",
  "ts":             1779470305
}
```

The `anon_id` is a random per-install UUID stored at `~/.webloom/anon_id`. It is NEVER linked to email, IP, identity, or any other identifier. Full data boundary documented at https://webloom.run/transparency.

Toggle any time:

```bash
python ~/.webloom/engine/server.py telemetry on        # opt in
python ~/.webloom/engine/server.py telemetry off       # opt out
python ~/.webloom/engine/server.py telemetry status    # current state
python ~/.webloom/engine/server.py telemetry preview   # see next payload without sending
```

## Code provenance

- **Source:** every line at https://github.com/webloomhq/engine
- **License:** MIT (see [LICENSE](./LICENSE))
- **Vendored deps in `vendor/`:** open-source third-party libs. Currently `x_client_transaction` (MIT, from [github.com/iSarabjitDhiman/XClientTransaction](https://github.com/iSarabjitDhiman/XClientTransaction)) — used by `x_create_tweet` to compute X's `x-client-transaction-id` header from publicly fetched bundle data.
- **Dependencies in `requirements.txt`:** all standard PyPI packages — `mcp`, `websockets`, `beautifulsoup4`, `psutil`, `pywin32` (Windows only). You can `cat requirements.txt` and verify each on PyPI before installing.

## Threat model

WebLoom is for users automating their **own** browser sessions on sites they're already authorized to use. It is not built for, and should not be used for:

- Bypassing authorization on accounts you don't control
- Mass scraping in violation of a site's ToS
- Distributing or running stolen credentials
- Anything that crosses the line into platform abuse

The engine has no opinions about what you do with it — but the **community Threads** on the marketplace are author-gated, and Threads that document abuse pattern get rejected at review.

## Disclosing vulnerabilities

Email **nanomarche@gmail.com**. We confirm receipt within 72 hours, fix within 14 days for high-severity issues, and publish the fix + write-up in CHANGELOG.md.
