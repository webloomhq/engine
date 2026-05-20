# Weaver — build & validate WebLoom Threads

One script. Two commands. No daemon, no schedule, no surprises.

```
python weaver.py discover https://example.com/        # first time on a site
python weaver.py check    example.com                  # re-validate later
python weaver.py list                                  # what's installed
```

Both modes are **read-only**. Nothing saves, submits, or changes site state.

---

## What a Thread is

A Thread is a JSON profile pack at `~/.webloom/threads/<domain>.thread.json` that captures everything WebLoom knows about one site: inputs, buttons, framework, fingerprints, anti-bot notes, and a frozen **pre-flight** — the same probes that discovered the site, kept around so we can detect drift later.

Threads are the unit you share, install, and sell on the WebLoom Atelier.

## How `discover` works

1. **Web-search bootstrap.** Before opening Chrome, Weaver searches DuckDuckGo for the domain on GitHub + Stack Overflow, fetches the top results, and extracts selector-shaped strings (`#ids`, `[name=...]`, `[data-*]`, `role=`, `aria-label`). These become *priors* — a starting hypothesis about what the page contains.
2. **Live probe.** Connects to your Chrome session over CDP, navigates to the URL, and probes: framework detection, visible inputs, visible buttons, durable fingerprints (body class, element counts, framework markers).
3. **Freeze.** The probes that returned real results are converted to a list of check specs (`selector_exists`, `js_check`, `button_text`, `framework_marker`) and saved as the Thread's `preflight` field.
4. **Write Thread.** Saved to `~/.webloom/threads/<domain>.thread.json`. Any prior version is backed up.

## How `check` works

Loads the Thread, navigates to its `seed_url`, replays every frozen probe, prints green/red. Writes a status file to `~/.webloom/logs/<domain>-check.json` for other scripts to consult.

When red, it tells you exactly how to repair: `python weaver.py discover <url>` to rebuild from scratch.

## When to run check

Whenever you want. Common moments:
- Before a production run (Weave) that depends on this Thread.
- After a site sends you "we updated our UI" notice.
- Periodically — say, weekly — if a site is important to you.

There's no background daemon. You're in charge of when.

## Login-gated sites

Weaver uses whatever Chrome tab is already open on `--port`. Log into the site once, in that tab; Weaver reuses your session.

## Anatomy of a Thread

```jsonc
{
  "domain": "example.com",
  "name": "example.com Thread",
  "version": "1.0.0",
  "created_at": 1779100000,
  "seed_url": "https://example.com/",
  "anti_bot": { "verdict": "normal" },
  "framework": { "frameworks": ["react-18"] },
  "fingerprints": { "body_class": "...", "forms_count": 1, "next_data": true },
  "inputs":  [ { "id": "email", "type": "email", "name": "email" } ],
  "buttons": [ { "text": "Sign in", "id": "submit" } ],
  "priors":  { "sources": ["https://github.com/..."], "candidate_selectors": ["#email"] },
  "preflight": [
    { "name": "framework_react-18",  "kind": "framework_marker", "probe": "react-18", "expected": "react-18 detected" },
    { "name": "input_email",         "kind": "selector_exists",  "probe": "#email",   "expected": "input #email" },
    { "name": "button_sign_in",      "kind": "button_text",      "probe": "sign in",  "expected": "button 'sign in'" }
  ]
}
```

The `preflight` field is the **same shape** for every Thread. Anyone can read it, anyone can re-run it, anyone can extend it.

## Why this is the right shape for the Atelier

A Thread that ships with its own pre-flight is **self-validating**. Buyers don't have to trust selectors that worked once on someone's machine — they can run `weaver check` and see green/red in 30 seconds. When a site drifts, the seller gets a clear repair signal: re-run discover, ship a new version.

The selectors are the commodity. The keep-it-green machinery is the moat.

## What discover is NOT

- Not a recipe runner. Threads describe the *shape* of a site. Weaves (separate concept) chain Threads together into end-to-end workflows.
- Not a magic crawler. It probes one page per run. To cover multi-step wizards, run `discover` on each step's URL — each becomes its own check group or its own Thread.
- Not a substitute for human judgment. The frozen probes are heuristics. Edit the Thread JSON if a check is noisy or missing something important.
