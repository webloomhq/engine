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

## Scanner audit posture

Third-party security scanners (MCP Marketplace, code-review bots, supply-chain trackers) sometimes flag WebLoom for patterns that look risky in isolation but are intentional product behaviour. Here's what each flag actually means in our case, and what we've hardened against.

### Subprocess execution

WebLoom launches Chrome via `subprocess.Popen([...args])` (list-form, never `shell=True`). The only user-controlled string ever passed to `Popen` is the `--url` argument, and even that is passed as a separate list element (not concatenated into a shell command). No injection vector.

We also call PowerShell once on Windows to bring a Chrome window to the foreground; the only interpolated value is an integer HWND from `ctypes.EnumWindows`, never user input.

### Path traversal

`install_thread` reads a `.thread.json` from any user-supplied path. The threat: a malicious `.thread.json` could carry a `"domain": "../../etc/evil"` field to escape `~/.webloom/threads/`. Hardened (2026-06-23): we now (a) validate the domain matches `[a-z0-9.\-]+` (DNS-shape, no slashes), max 253 chars, and (b) resolve the destination path and verify it stays inside `THREADS_DIR.resolve()`. Anything that escapes is refused.

`export_profile` and `import_profile` accept full user-supplied paths because the user is explicitly choosing where to write/read their own cookie export. That's not a traversal vector — it's the user choosing their own filesystem location.

### Auto-record log (`~/.webloom/auto_recording.jsonl`)

The engine records every action tool call to a local rolling JSONL (rotating at 10k entries) so authors can convert any session window into a draft Thread. Naive logging would put `fill`/`key_type` text + `xhr_upload` headers on disk in plaintext. Hardened (2026-06-23):

- Top-level args whose key matches `(password|passwd|secret|token|api_key|apikey|authorization|auth|cookie|session|private_key|credential|bearer|passphrase)` are replaced with `[REDACTED]` before write.
- Nested dicts (e.g. `fill.fields`, `xhr_upload.headers`) are walked the same way — any matching key gets `[REDACTED]`.
- On POSIX, the log file is `chmod 0600` after each write so other local users can't read it. (Windows uses NTFS ACLs; the default user profile permissions already exclude other users.)
- Opt-out: `WEBLOOM_AUTO_RECORD=off` disables auto-record entirely.

Authors should still avoid typing real passwords through automation — for credential entry, use the browser's password manager or `pause_for_human` and let the user type directly.

### API key handling

`vision_check`, `weave`, and `swarm_run` use **the user's own `ANTHROPIC_API_KEY`** read from environment. The key is sent in the `x-api-key` header **directly to Anthropic's API** (`api.anthropic.com`). It never touches webloom.run, the engine's telemetry endpoints, or any third party. Never logged, never in telemetry payloads, never in the auto-record log (its env-only lifetime means it never appears in a tool argument).

`solve_captcha` uses the user's own `CAPTCHA_API_KEY` (2Captcha or similar). Same posture: env-only, sent direct to the captcha provider, never logged.

### Telemetry

Telemetry is **opt-in, off by default**. When enabled, two payload types ship:

1. Per-tool engine telemetry: `{tool, ok, error_class, duration_ms, engine_version, anon_id, ts}` — no URLs, no content, no creds.
2. Per-action marketplace telemetry: `{anon_id, domain, action_descriptor, strategy, confidence, ok, verified, verify_kind, ms, engine_version}` — domain is the site name (e.g. `kdp.amazon.com`), descriptor is a selector hint (e.g. `click:button[type=submit]`), strategy is the named strategy that worked. No credentials, no cookies, no page content.

The `anon_id` is a random per-install UUID never linked to identity.

### Network timeouts

All `urllib.request.urlopen` calls have explicit timeouts (range: 3-30 seconds depending on operation). No infinite-hang vectors.

### Supply chain

Dependencies pinned to lower-bound versions known to be CVE-clean as of 2026-06-23:

```
mcp>=1.0.0
python-dotenv>=1.0.0
websockets>=12.0
pyotp>=2.9.0
psutil>=5.9.0
pywin32>=306; sys_platform == "win32"
beautifulsoup4>=4.12.0
```

The `fastmcp` dependency was removed in v0.3.5 along with the vestigial `server_fastmcp.py` entrypoint. The `pyautogui` dependency was removed in v0.3.0 along with the `real_cursor_click` function. The active engine never moves the OS cursor.

Vendored libs in `vendor/x_client_transaction/` are pinned to the upstream commit at vendoring time, MIT-licensed, audit-readable, no transitive deps.

## Disclosing vulnerabilities

Email **nanomarche@gmail.com**. We confirm receipt within 72 hours, fix within 14 days for high-severity issues, and publish the fix + write-up in CHANGELOG.md.
