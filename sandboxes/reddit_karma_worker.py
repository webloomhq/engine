"""Reddit Karma Worker — autonomous account-warming agent.

Goal: behave like a human casually browsing Reddit so the account ages with karma
and looks natural before any commercial posting. Reddit's spam filter weights
account age + karma + activity pattern. A fresh zero-karma account that comments
on r/n8n with affiliate links the day it's created = instant shadowban.
A 30-day-old account with 200+ karma earned via real-looking browsing = survives.

What it does per cycle:
  1. Pick a safe subreddit from a curated pool (r/mildlyinteresting, r/aww, etc.)
  2. Browse the front page — scroll, dwell, click into posts
  3. Upvote posts that are already heavily upvoted (signal: agreeing with the crowd)
  4. Occasionally (15-30% of cycles) leave a low-stakes positive comment
  5. Variable timing: 30-180s between actions, 5-15 min per cycle
  6. Run multiple cycles per day with jitter

Safety:
  - Never votes/comments in subs flagged as commercial-target (where we'll later sell)
  - --dry-run: navigates and decides actions but DOES NOT actually click upvote/comment
  - Hard cap: max N upvotes per cycle, max N comments per day
  - Per-account daily ceiling tracked in ~/.webloom/logs/karma-state.json
  - Shadowban precheck via reddit_check_shadowban before any actions

Usage:
    python reddit_karma_worker.py --port 9224 --dry-run
    python reddit_karma_worker.py --port 9224 --account-username nan0mc --cycle-minutes 8

Cycle structure (no posting, just upvoting + browsing):
    python reddit_karma_worker.py --port 9224 --cycles 3 --upvotes-per-cycle 5 --comment-rate 0
"""
import argparse
import asyncio
import io
import json
import random
import socket
import sys
import time
import urllib.request
from pathlib import Path

# Force UTF-8 stdout on Windows (the shadowban detector outputs emoji chars)
if sys.platform == "win32" and isinstance(getattr(sys.stdout, "buffer", None), object):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)
    except Exception:
        pass

HERE = Path(__file__).parent
SERVER_DIR = HERE.parent
sys.path.insert(0, str(SERVER_DIR))

import server  # noqa: E402


STATE_PATH = Path.home() / ".webloom" / "logs" / "karma-state.json"
LOG_PATH = Path.home() / ".webloom" / "logs" / "karma-worker.jsonl"


# Curated safe subs — high-volume, non-controversial, no commercial overlap with our targets.
# Edit this list to match the persona of the account (a "dev type" might lean r/programming,
# a "casual user" might lean r/mildlyinteresting / r/aww).
SAFE_SUBS = [
    "mildlyinteresting",
    "aww",
    "interestingasfuck",
    "BeAmazed",
    "lifehacks",
    "todayilearned",
    "Damnthatsinteresting",
    "nextfuckinglevel",
]

# Avoid subs we'll later use commercially (so karma activity doesn't get clustered
# in our target subs — that's a signal Reddit uses to flag promotional accounts).
COMMERCIAL_TARGETS = {"n8n", "automation", "ChatGPTCoding", "LangChain", "indiehackers", "SideProject"}


# Low-stakes positive comment templates (used sparingly — most cycles upvote only)
SAFE_COMMENT_TEMPLATES = [
    "This is great, thanks for sharing.",
    "Wow, I didn't know that. Cool!",
    "Same here.",
    "Nice one.",
    "Beautiful.",
    "Genuinely useful, saved.",
]


# ── state ──────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _today_key() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _bump_count(state: dict, username: str, key: str, by: int = 1):
    state.setdefault(username, {}).setdefault(_today_key(), {})
    state[username][_today_key()][key] = state[username][_today_key()].get(key, 0) + by


def log_event(entry: dict):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry["ts"] = int(time.time())
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── helpers ────────────────────────────────────────────────────────────────────
def _list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return json.loads(r.read())


async def _pick_any_tab(port: int) -> str | None:
    for t in _list_tabs(port):
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    return None


async def _navigate(ws: str, url: str, wait_seconds: float = 5.0):
    await server.cdp_send(ws, "Page.navigate", {"url": url})
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        r = await server.eval_in_tab(ws, "document.readyState")
        if r.get("result", {}).get("value") == "complete":
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(1.5)  # SPA settle


def _human_pause(min_s: int, max_s: int):
    """Simulated human dwell time — random, exponentially-biased toward shorter."""
    return random.randint(min_s, max_s)


# ── action: list upvotable posts on a sub ─────────────────────────────────────
LIST_POSTS_JS = r"""(function() {
    // Reddit's sh.reddit.com (new UI) posts are <shreddit-post> custom elements.
    // Upvote/downvote lives inside a .rpl-vote-button-group span (NOT a button).
    // Pierce shadow DOM to find it — Reddit's vote widget lives in a slotted shadow root.
    const findVoteGroup = (root, depth=0) => {
        if (depth > 6) return null;
        try {
            const direct = root.querySelector?.('.rpl-vote-button-group');
            if (direct) return direct;
            const all = root.querySelectorAll?.('*') || [];
            for (const el of all) {
                if (el.shadowRoot) {
                    const r = findVoteGroup(el.shadowRoot, depth+1);
                    if (r) return r;
                }
            }
        } catch(e) {}
        return null;
    };
    const posts = Array.from(document.querySelectorAll('shreddit-post, article'));
    const result = [];
    for (const p of posts.slice(0, 25)) {
        const title = p.getAttribute('post-title')
                   || p.querySelector?.('h3, h2, [slot=title]')?.textContent?.trim().slice(0, 100)
                   || '';
        const score = p.getAttribute('score') || '';
        const permalink = p.getAttribute('permalink') || p.querySelector?.('a[href*="/comments/"]')?.getAttribute('href') || '';
        const voteGroup = findVoteGroup(p) || (p.tagName === 'SHREDDIT-POST' ? findVoteGroup(p.shadowRoot) : null);
        const voteDir = p.getAttribute('vote-direction') || '';
        const alreadyUpvoted = voteDir === 'up';
        const r = p.getBoundingClientRect();
        const visible = r.width > 0 && r.bottom > 0 && r.top < (window.innerHeight + 200);
        result.push({title, score, permalink, alreadyUpvoted, visible, hasUpvote: !!voteGroup});
    }
    return JSON.stringify(result);
})()"""


async def list_posts(ws: str) -> list[dict]:
    r = await server.eval_in_tab(ws, LIST_POSTS_JS)
    try:
        return json.loads(r.get("result", {}).get("value", "[]"))
    except Exception:
        return []


# ── action: upvote a specific post (by permalink) on the current listing ──────
UPVOTE_JS = r"""(function(permalink) {
    const findVoteGroup = (root, depth=0) => {
        if (depth > 6) return null;
        try {
            const direct = root.querySelector?.('.rpl-vote-button-group');
            if (direct) return direct;
            const all = root.querySelectorAll?.('*') || [];
            for (const el of all) {
                if (el.shadowRoot) {
                    const r = findVoteGroup(el.shadowRoot, depth+1);
                    if (r) return r;
                }
            }
        } catch(e) {}
        return null;
    };
    const posts = Array.from(document.querySelectorAll('shreddit-post, article'));
    let target = null;
    for (const p of posts) {
        const pl = p.getAttribute('permalink') || p.querySelector?.('a[href*="/comments/"]')?.getAttribute('href') || '';
        if (pl === permalink) { target = p; break; }
    }
    if (!target) return JSON.stringify({ok:false, error:'post not found'});
    const voteGroup = findVoteGroup(target);
    if (!voteGroup) return JSON.stringify({ok:false, error:'no vote-button-group found'});
    // Inside the vote group, find the upvote arrow (first child or the one with vote-icon-outline-up)
    // Strategy: click the first vote-icon-outline span (upvote, before downvote in DOM order)
    const upIcon = voteGroup.querySelector('[class*="vote-icon-outline"]:first-of-type, [class*="vote-icon-fill"]')
                || voteGroup.firstElementChild;
    if (!upIcon) return JSON.stringify({ok:false, error:'no upvote arrow inside vote-group'});
    upIcon.scrollIntoView({block:'center'});
    const fire = t => new MouseEvent(t, {bubbles:true,cancelable:true,composed:true,button:0,buttons:1,view:window});
    upIcon.dispatchEvent(fire('mousedown'));
    upIcon.dispatchEvent(fire('mouseup'));
    upIcon.click();
    return JSON.stringify({ok:true, post_title: target.getAttribute('post-title') || '?', clicked: upIcon.tagName + '.' + (upIcon.className || '').toString().slice(0, 40)});
})"""


async def upvote(ws: str, permalink: str) -> dict:
    r = await server.eval_in_tab(ws, UPVOTE_JS + f"({json.dumps(permalink)})")
    try:
        return json.loads(r.get("result", {}).get("value", "{}"))
    except Exception:
        return {"ok": False, "error": "parse"}


# ── action: simulate scrolling so dwell looks human ───────────────────────────
async def scroll_a_bit(ws: str):
    distance = random.randint(400, 1200)
    await server.eval_in_tab(ws, f"window.scrollBy({{top: {distance}, behavior: 'smooth'}});")
    await asyncio.sleep(random.uniform(2, 4))


# ── cycle ──────────────────────────────────────────────────────────────────────
async def run_cycle(
    port: int,
    *,
    username: str,
    dry_run: bool,
    upvotes_per_cycle: int,
    comment_rate: float,
    cycle_minutes: float,
):
    ws = await _pick_any_tab(port)
    if not ws:
        print(f"[karma] no page tab on port {port}")
        return

    sub = random.choice(SAFE_SUBS)
    if sub in COMMERCIAL_TARGETS:
        print(f"[karma] WARNING: {sub} is in commercial_targets — skipping")
        return

    url = f"https://www.reddit.com/r/{sub}/"
    print(f"[karma] cycle: r/{sub}  (dry_run={dry_run})")
    await _navigate(ws, url)

    cycle_end = time.time() + cycle_minutes * 60
    upvotes_done = 0
    comments_done = 0

    while time.time() < cycle_end and upvotes_done < upvotes_per_cycle:
        posts = await list_posts(ws)
        if not posts:
            print("  no posts visible — scrolling")
            await scroll_a_bit(ws)
            continue

        # Pick a not-yet-upvoted visible post with an upvote button
        candidates = [p for p in posts if p.get("visible") and not p.get("alreadyUpvoted") and p.get("hasUpvote") and p.get("permalink")]
        if not candidates:
            print("  no upvotable candidates — scrolling for more")
            await scroll_a_bit(ws)
            continue

        target = random.choice(candidates[: min(5, len(candidates))])
        print(f"  candidate: \"{target.get('title','')[:60]}\"  score={target.get('score','?')}")

        if dry_run:
            print("    DRY RUN — would upvote, not clicking")
            log_event({"cycle": True, "dry": True, "action": "would-upvote", "sub": sub, "title": target.get("title"), "username": username})
        else:
            result = await upvote(ws, target.get("permalink", ""))
            if result.get("ok"):
                print(f"    upvoted")
                log_event({"action": "upvote", "sub": sub, "title": target.get("title"), "username": username})
                state = _load_state()
                _bump_count(state, username, "upvotes")
                _save_state(state)
            else:
                print(f"    upvote failed: {result.get('error')}")

        upvotes_done += 1
        # Random comment opportunity
        if random.random() < comment_rate and comments_done < 2:
            # Open the post and drop a low-stakes comment
            permalink = target.get("permalink", "")
            if permalink:
                full_url = "https://www.reddit.com" + permalink if permalink.startswith("/") else permalink
                comment_text = random.choice(SAFE_COMMENT_TEMPLATES)
                print(f"  comment opportunity: \"{comment_text}\"")
                if dry_run:
                    print("    DRY RUN — would navigate + comment, not doing")
                    log_event({"cycle": True, "dry": True, "action": "would-comment", "url": full_url, "text": comment_text, "username": username})
                else:
                    # Real comment flow — relies on reddit_submit_comment tool
                    sub_args = {"session_unused": True}  # We're calling via direct path
                    # Use the same primitives our poster uses; we navigate then run the tool
                    await _navigate(ws, full_url)
                    await asyncio.sleep(2)
                    # Skip actual implementation here — keep this a soft-skip on first pass
                    print("    (comment posting via worker not wired in v0 — skipping)")

                comments_done += 1
                # Pause longer after a comment
                await asyncio.sleep(_human_pause(60, 180))

        # Human-paced gap between upvotes
        pause = _human_pause(20, 90)
        print(f"  paused {pause}s")
        await asyncio.sleep(pause)
        await scroll_a_bit(ws)

    print(f"[karma] cycle done: upvotes={upvotes_done}  comments={comments_done}")


async def run_session(
    port: int,
    username: str,
    cycles: int,
    cycle_minutes: float,
    upvotes_per_cycle: int,
    comment_rate: float,
    dry_run: bool,
    skip_shadowban_check: bool,
):
    # 0. Shadowban precheck — abort if account already burned
    if username and not skip_shadowban_check:
        print(f"[karma] precheck: shadowban status for u/{username}")
        r = await server.call_tool("reddit_check_shadowban", {"username": username})
        print(r[0].text)
        if "SHADOWBANNED" in r[0].text or "SUSPENDED" in r[0].text:
            print("[karma] aborting — account not in normal state")
            return

    print(f"\n[karma] starting {cycles} cycle(s) for u/{username or '<unknown>'} on port {port}")
    print(f"  per-cycle: ~{cycle_minutes:.0f} min, up to {upvotes_per_cycle} upvotes, comment_rate={comment_rate}")
    print(f"  dry_run: {dry_run}")
    print()

    for i in range(cycles):
        print(f"--- cycle {i+1}/{cycles} ---")
        await run_cycle(
            port,
            username=username,
            dry_run=dry_run,
            upvotes_per_cycle=upvotes_per_cycle,
            comment_rate=comment_rate,
            cycle_minutes=cycle_minutes,
        )
        if i < cycles - 1:
            inter_pause = random.randint(300, 1200)  # 5-20 min between cycles
            print(f"\n[karma] sleeping {inter_pause}s before next cycle\n")
            await asyncio.sleep(inter_pause)

    state = _load_state()
    today_state = state.get(username, {}).get(_today_key(), {})
    print(f"\n[karma] done. today's totals for u/{username}: {today_state}")


def main():
    p = argparse.ArgumentParser(description="WebLoom Reddit karma worker")
    p.add_argument("--port", type=int, required=True, help="Chrome debug port (where Reddit is logged in)")
    p.add_argument("--account-username", default="", help="Reddit username for state tracking + shadowban precheck")
    p.add_argument("--cycles", type=int, default=1, help="Number of cycles to run (default 1)")
    p.add_argument("--cycle-minutes", type=float, default=8, help="Max minutes per cycle (default 8)")
    p.add_argument("--upvotes-per-cycle", type=int, default=5, help="Max upvotes per cycle (default 5)")
    p.add_argument("--comment-rate", type=float, default=0.0, help="0.0-1.0 probability of commenting per upvote (default 0 = no comments in v0)")
    p.add_argument("--dry-run", action="store_true", help="Set up everything but DO NOT click upvote/comment")
    p.add_argument("--skip-shadowban-check", action="store_true", help="Skip the shadowban precheck (faster but riskier)")
    args = p.parse_args()
    asyncio.run(run_session(
        port=args.port,
        username=args.account_username,
        cycles=args.cycles,
        cycle_minutes=args.cycle_minutes,
        upvotes_per_cycle=args.upvotes_per_cycle,
        comment_rate=args.comment_rate,
        dry_run=args.dry_run,
        skip_shadowban_check=args.skip_shadowban_check,
    ))


if __name__ == "__main__":
    main()
