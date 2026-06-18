"""
WebLoom marketplace client — in-chat browse / install / library / bounties.

All payment-touching state lives on webloom.run (Supabase + Polar). This
module just talks to those endpoints with a paired session token.

Session token storage: ~/.webloom/auth.json, mode 0600 on POSIX. Token is
local-only — never sent anywhere except webloom.run over HTTPS.

Pairing flow:
  1. webloom_pair() — engine asks webloom.run for a short code, prints it +
     a URL, polls /api/auth/pair/claim until the user confirms in their
     browser. Token saved to auth.json.
  2. Every other marketplace tool reads auth.json and includes the token in
     its POST body. 401 → tool tells the user to run webloom_pair() again.

The engine NEVER holds payment data. webloom_install asks the human to
confirm the price; only on confirmation does the engine open the checkout
URL in their default browser.
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

# --- network targets (env-overridable for staging / self-hosted) ----------
WEBLOOM_BASE_URL = os.environ.get("WEBLOOM_BASE_URL", "https://webloom.run")
PAIR_INIT_URL = f"{WEBLOOM_BASE_URL}/api/auth/pair/init"
PAIR_CLAIM_URL = f"{WEBLOOM_BASE_URL}/api/auth/pair/claim"
BROWSE_URL = f"{WEBLOOM_BASE_URL}/api/marketplace/browse"
THREAD_INFO_URL_BASE = f"{WEBLOOM_BASE_URL}/api/marketplace/thread"
INSTALL_URL = f"{WEBLOOM_BASE_URL}/api/marketplace/install"
LIBRARY_URL = f"{WEBLOOM_BASE_URL}/api/marketplace/library"
BOUNTIES_URL = f"{WEBLOOM_BASE_URL}/api/marketplace/bounties"
MY_THREADS_URL = f"{WEBLOOM_BASE_URL}/api/author/my-threads"
CLAIM_BOUNTY_URL = f"{WEBLOOM_BASE_URL}/api/marketplace/claim-bounty"
PUBLISH_THREAD_URL = f"{WEBLOOM_BASE_URL}/api/marketplace/publish-thread"

AUTH_FILE = Path.home() / ".webloom" / "auth.json"
THREADS_DIR = Path.home() / ".webloom" / "threads"
USER_AGENT = "webloom-engine/0.3.0"


# --- session token persistence -------------------------------------------
def _load_auth() -> dict:
    try:
        if AUTH_FILE.exists():
            return json.loads(AUTH_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_auth(d: dict):
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(d, indent=2))
    try:
        os.chmod(AUTH_FILE, 0o600)
    except Exception:
        pass


def get_session_token() -> str | None:
    return _load_auth().get("session_token")


def get_user_email() -> str | None:
    return _load_auth().get("user_email")


def clear_session():
    if AUTH_FILE.exists():
        AUTH_FILE.unlink()


# --- HTTP helpers --------------------------------------------------------
def _post(url: str, body: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """POST JSON, parse JSON response. Returns (status_code, body_dict).
    On JSON parse failure, body_dict = {'error': '<raw text>'}."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"error": raw[:400]}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode()
        except Exception:
            pass
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw[:400] or str(e)}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


# --- public tool implementations ----------------------------------------
def tool_pair(anon_id: str, poll_seconds: int = 180, open_browser: bool = True) -> dict:
    """Initiate pairing. Returns instructions + polls for confirmation.

    The MCP wrapper prints the code + URL, optionally opens the browser, and
    blocks polling until the user confirms or the timeout elapses. Both sides
    have a 10-minute server-side TTL so we never poll forever.
    """
    existing = get_session_token()
    if existing:
        return {
            "ok": True,
            "already_paired": True,
            "user_email": get_user_email(),
            "message": "Engine is already paired. To re-pair as a different user run webloom_pair_reset() first.",
        }

    status, body = _post(PAIR_INIT_URL, {"anon_id": anon_id})
    if status != 200 or not body.get("ok"):
        return {"ok": False, "error": f"pair_init failed: {body.get('error', body)}"}

    code = body["code"]
    pair_url = body["pair_url"]
    if open_browser:
        try:
            webbrowser.open(pair_url)
        except Exception:
            pass

    deadline = time.time() + max(30, min(poll_seconds, 600))
    while time.time() < deadline:
        s, b = _post(PAIR_CLAIM_URL, {"anon_id": anon_id, "code": code})
        if s == 200 and b.get("paired"):
            _save_auth({
                "session_token": b["session_token"],
                "user_email": b.get("user_email"),
                "anon_id": anon_id,
                "paired_at": int(time.time()),
            })
            return {
                "ok": True,
                "paired": True,
                "user_email": b.get("user_email"),
                "message": f"Paired to webloom.run as {b.get('user_email')}. You can now browse, install, and manage Threads from inside chat.",
            }
        if s == 200 and b.get("expired"):
            return {"ok": False, "error": "Pairing code expired before the user confirmed. Run webloom_pair() again."}
        time.sleep(3)
    return {
        "ok": False,
        "error": "Timed out waiting for the user to confirm pairing in the browser.",
        "code": code,
        "pair_url": pair_url,
        "hint": "Re-run webloom_pair() to get a fresh code, or finish the sign-in in your browser and try again.",
    }


def tool_browse(q: str = "", tier: str = "", framework: str = "", category: str = "", limit: int = 20) -> dict:
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() to link this engine to your webloom.run account."}
    payload: dict[str, Any] = {"session_token": token, "limit": limit}
    if q: payload["q"] = q
    if tier: payload["tier"] = tier
    if framework: payload["framework"] = framework
    if category: payload["category"] = category
    s, body = _post(BROWSE_URL, payload)
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired or revoked", "hint": "Run webloom_pair() to re-pair."}
    return body


def tool_thread_info(domain: str) -> dict:
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    url = f"{THREAD_INFO_URL_BASE}/{urllib.parse.quote(domain, safe='')}"
    s, body = _post(url, {"session_token": token})
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    return body


def tool_install(domain: str, confirm_paywall: bool = False, open_checkout: bool = True) -> dict:
    """Install a Thread:
      - Free → writes thread JSON to ~/.webloom/threads/.
      - Owned → writes thread JSON.
      - Paywall → returns price + checkout URL. If confirm_paywall=True AND
        open_checkout=True, opens the checkout in the default browser; the
        caller (the chat model) is expected to have shown the price and got
        user consent BEFORE setting confirm_paywall=True.
    """
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    s, body = _post(INSTALL_URL, {"session_token": token, "domain": domain})
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    if not body.get("ok"):
        return body

    status = body.get("status")
    if status in ("free", "owned"):
        thread = body.get("thread") or {}
        if thread.get("domain"):
            try:
                THREADS_DIR.mkdir(parents=True, exist_ok=True)
                out = THREADS_DIR / f"{thread['domain']}.thread.json"
                out.write_text(json.dumps(thread, indent=2))
                return {"ok": True, "status": status, "installed_to": str(out), "domain": thread["domain"]}
            except Exception as e:
                return {"ok": False, "error": f"download succeeded but write failed: {e}"}
        return body

    if status == "paywall":
        if confirm_paywall and open_checkout and body.get("checkout_url"):
            try:
                webbrowser.open(body["checkout_url"])
                body["browser_opened"] = True
            except Exception:
                body["browser_opened"] = False
        return body

    return body


def tool_library() -> dict:
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    s, body = _post(LIBRARY_URL, {"session_token": token})
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    return body


def tool_open_bounties(category: str = "", framework: str = "", limit: int = 25) -> dict:
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    payload: dict[str, Any] = {"session_token": token, "limit": limit}
    if category: payload["category"] = category
    if framework: payload["framework"] = framework
    s, body = _post(BOUNTIES_URL, payload)
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    return body


def tool_my_threads() -> dict:
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    s, body = _post(MY_THREADS_URL, {"session_token": token})
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    return body


def tool_claim_bounty(domain: str) -> dict:
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    if not domain:
        return {"ok": False, "error": "domain required"}
    s, body = _post(CLAIM_BOUNTY_URL, {"session_token": token, "domain": domain})
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    return body


def tool_publish_thread(domain: str, thread_path: str | None = None) -> dict:
    """Submit a Thread JSON file for admin review.

    thread_path defaults to ~/.webloom/threads/<domain>.thread.json — the
    standard location authors build to. The Thread is read, validated
    locally, then POSTed to webloom.run. It lands in `published_thread_drafts`
    awaiting admin approval; the engine returns a draft_id + review_url.
    """
    token = get_session_token()
    if not token:
        return {"ok": False, "error": "not paired", "hint": "Call webloom_pair() first."}
    if not domain:
        return {"ok": False, "error": "domain required"}
    path = Path(thread_path) if thread_path else THREADS_DIR / f"{domain}.thread.json"
    if not path.exists():
        return {"ok": False, "error": f"thread file not found at {path}", "hint": "Build the Thread first or pass thread_path explicitly."}
    try:
        thread = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"failed to read/parse thread: {e}"}
    if thread.get("domain") and thread["domain"].lower() != domain.lower():
        return {"ok": False, "error": f"thread.domain ({thread.get('domain')}) does not match domain arg ({domain})"}
    s, body = _post(PUBLISH_THREAD_URL, {
        "session_token": token,
        "domain": domain,
        "thread": thread,
    }, timeout=20.0)
    if s == 401:
        clear_session()
        return {"ok": False, "error": "session expired", "hint": "Run webloom_pair() to re-pair."}
    return body
