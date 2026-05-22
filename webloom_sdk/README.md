# WebLoom SDK

Call the engine from non-MCP Python.

The MCP server exposes 70+ browser-control tools. This SDK wraps them as a
plain Python class so you can drive WebLoom from cron jobs, CI workflows,
scripts, or any server-side code.

## Install

The SDK ships with the engine. After `git clone https://github.com/webloomhq/engine`:

```bash
# Option A: install as editable package
cd ~/.webloom/engine && pip install -e .

# Option B: add the dir to PYTHONPATH
export PYTHONPATH=$HOME/.webloom/engine:$PYTHONPATH
```

## Quick start

```python
from webloom_sdk import Session

with Session("slot-1") as s:
    s.launch_session()                       # start Chrome with debug port
    r = s.new_tab(url="https://example.com")
    s.click("a.cta")
    s.fill("#email", "you@example.com")
    s.key_press("Enter")
    # All 70+ tools exposed as methods
```

## Vision + click pattern

```python
v = s.vision_check(question="click coords for the post button")
# v == {"ok": True, "answer": "...", "click": {"x": 720, "y": 480}}
if v.get("click"):
    s.click_at_coords(x=v["click"]["x"], y=v["click"]["y"])
```

## Parallel fanout

```python
res = s.run_parallel(calls=[
    {"tool": "scan_tab", "args": {"session": "slot-1", "tab": "tab1"}},
    {"tool": "scan_tab", "args": {"session": "slot-1", "tab": "tab2"}},
    {"tool": "scan_tab", "args": {"session": "slot-1", "tab": "tab3"}},
], max_concurrency=3)
# res == {"ok": True, "count": 3, "results": [...]}
```

## Drift-healing a broken selector

```python
hints = s.drift_heal_suggest(
    old_selector='[data-testid="old-id"]',
    descriptor="post tweet button",
)
# hints == {"ok": True, "candidates": [{"selector": "...", "score": 7, ...}]}
```

## Calling any tool by name

The shortcut methods cover the common cases. For anything else:

```python
text = s.call("eval_js", code="document.title")    # returns text
data = s.call_json("vision_check", question="...") # returns parsed JSON
```

## Environment

| Var | Purpose |
|---|---|
| `WEBLOOM_ENGINE` | Path to `server.py` (auto-detects `~/.webloom/engine/server.py` if unset) |
| `ANTHROPIC_API_KEY` | Required for `vision_check`. |
| `CAPTCHA_PROVIDER` / `CAPTCHA_API_KEY` | Required for `solve_captcha` (currently 2captcha). |

## Notes

- Each `Session()` spawns one child process. Reuse it for many calls — the JSON-RPC handshake is set up once.
- `default_session` is the Chrome slot the tools target. Override per-call with `s.click(selector=..., session="slot-2")`.
- The SDK is synchronous. For async use, wrap calls in `asyncio.to_thread()`.
- All tool errors raise `WebLoomError` with the engine's error message.
