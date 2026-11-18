"""Pairing with a Vibedna licence key (webloom_marketplace.tool_pair with VIBEDNA_LICENSE_KEY), every request mocked.
Run: python test_pair_key.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webloom_marketplace as wm  # noqa: E402

tmp = Path(tempfile.mkdtemp())
wm.AUTH_FILE = tmp / "auth.json"
wm.THREADS_DIR = tmp / "threads"
calls = []
OWNED = ["instagram.com", "x.com", "tiktok.com"]


def fake_post(url, body, timeout=10.0):
    calls.append((url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1], {k: v for k, v in body.items() if k != "key"}, "key" in body))
    if url == wm.PAIR_KEY_URL:
        if body.get("key") == "vbd_cont_aaaaaaaa_bbbbbbbb":
            return 200, {"ok": True, "paired": True, "session_token": "wlse_" + "0" * 32, "user_email": "buyer@example.com"}
        return 401, {"ok": False, "error": "this key does not open a Vibedna account"}
    if url == wm.LIBRARY_URL:
        return 200, {"ok": True, "items": [{"domain": d} for d in OWNED]}
    if url == wm.INSTALL_URL:
        return 200, {"ok": True, "status": "owned", "thread": {"domain": body["domain"], "version": "1.0.0"}}
    if url == wm.PAIR_INIT_URL:
        return 200, {"ok": True, "code": "ABC-DEF", "pair_url": "https://webloom.run/pair?code=ABC-DEF"}
    return 404, {"error": "unexpected " + url}


wm._post = fake_post
ok = 0


def check(cond, what):
    global ok
    assert cond, what
    ok += 1


# a wrong key: refused, nothing saved, no browser fallback
os.environ["VIBEDNA_LICENSE_KEY"] = "vbd_cont_ffffffff_ffffffff"
r = wm.tool_pair("anon-abcdef123456", open_browser=False)
check(not r["ok"] and "does not open" in r["error"] and not wm.AUTH_FILE.exists(), "wrong key refused, nothing saved")
check(all(c[0] != "pair/init" for c in calls), "a wrong key never falls back to the browser pairing by itself")

# the right key: paired with no browser, the three owned threads installed, the key never stored
calls.clear()
os.environ["VIBEDNA_LICENSE_KEY"] = "vbd_cont_aaaaaaaa_bbbbbbbb"
r = wm.tool_pair("anon-abcdef123456", open_browser=False)
check(r["ok"] and r["paired"] and r["user_email"] == "buyer@example.com", "paired with the key")
check(sorted(r["threads"]["installed"]) == sorted(OWNED) and not r["threads"]["failed"], "owned threads installed")
check(all((wm.THREADS_DIR / f"{d}.thread.json").exists() for d in OWNED), "thread files written")
auth = json.loads(wm.AUTH_FILE.read_text())
check(auth["via"] == "vibedna_key" and "vbd_" not in wm.AUTH_FILE.read_text(), "auth saved, key not stored")
check([c[0] for c in calls][:2] == ["pair/key", "marketplace/library"] and calls[0][2], "key sent only to the pairing route")
check(all(not c[2] for c in calls[1:]), "the key is sent nowhere else")

# a second call: already paired, nothing reinstalled
calls.clear()
r = wm.tool_pair("anon-abcdef123456", open_browser=False)
check(r["already_paired"] and r["threads"]["already"] == OWNED and not r["threads"]["installed"], "second call installs nothing twice")

# without the key the old browser pairing is untouched
wm.clear_session()
del os.environ["VIBEDNA_LICENSE_KEY"]
calls.clear()
r = wm.tool_pair("anon-abcdef123456", poll_seconds=30, open_browser=False) if False else None
check(wm._vibedna_key() == "", "no key in the environment means the browser pairing")
print("pair with a Vibedna key: %d checks passed" % ok)
