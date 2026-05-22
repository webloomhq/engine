"""WebLoom SDK client — JSON-RPC over stdio to the engine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class WebLoomError(Exception):
    pass


class Session:
    """Spawns a webloom MCP server subprocess and lets you call any of its tools.

    Each Session owns one child process. The session arg identifies which
    Chrome slot the engine should attach to (slot-1, slot-2, etc.) — the
    same identifier you'd use through the MCP host.

    Example:
        with Session("slot-1") as s:
            s.launch_session()
            r = s.new_tab(url="https://example.com")
            tab_id = r["tab_id"]
            s.click(selector="a.cta", session="slot-1", tab=tab_id)
    """

    def __init__(self, default_session: str = "slot-1", engine_path: str | None = None, python_bin: str | None = None):
        self.default_session = default_session
        self.engine_path = engine_path or self._locate_engine()
        self.python_bin = python_bin or sys.executable
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._init_done = False

    def _locate_engine(self) -> str:
        # 1. WEBLOOM_ENGINE env var
        env_path = os.environ.get("WEBLOOM_ENGINE")
        if env_path and Path(env_path).exists():
            return env_path
        # 2. ~/.webloom/engine/server.py (canonical install)
        home = Path.home() / ".webloom" / "engine" / "server.py"
        if home.exists():
            return str(home)
        # 3. sibling server.py (when SDK lives next to the engine)
        sibling = Path(__file__).resolve().parent.parent / "server.py"
        if sibling.exists():
            return str(sibling)
        raise WebLoomError(
            "Could not locate engine. Set WEBLOOM_ENGINE=/path/to/server.py "
            "or install the engine to ~/.webloom/engine/."
        )

    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, *a) -> None:
        self.stop()

    def start(self) -> None:
        if self._proc:
            return
        self._proc = subprocess.Popen(
            [self.python_bin, self.engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(Path(self.engine_path).parent),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        # JSON-RPC initialize handshake
        self._send_raw({
            "jsonrpc": "2.0",
            "id": self._take_id(),
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "webloom-sdk", "version": "0.1"}},
        })
        self._read_response()
        # Send initialized notification
        self._send_raw({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._init_done = True

    def stop(self) -> None:
        if not self._proc:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()
        self._proc = None

    def _take_id(self) -> int:
        with self._lock:
            i = self._next_id
            self._next_id += 1
            return i

    def _send_raw(self, msg: dict) -> None:
        if not self._proc or self._proc.stdin is None:
            raise WebLoomError("Process not started")
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _read_response(self, timeout: float = 60.0) -> dict:
        if not self._proc or self._proc.stdout is None:
            raise WebLoomError("Process not started")
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except Exception:
                continue
            # Skip notifications, only return responses with an id
            if "id" in msg:
                return msg
        raise WebLoomError("Timeout waiting for response")

    def call(self, tool: str, **args: Any) -> str:
        """Call any webloom tool. Returns the tool's text output.

        Defaults `session` to self.default_session if not in args.
        Returns the raw text from the tool's TextContent (callers can parse
        further if the tool returns JSON in its text).
        """
        if not self._init_done:
            self.start()
        args.setdefault("session", self.default_session)
        req_id = self._take_id()
        self._send_raw({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
        resp = self._read_response()
        if "error" in resp:
            raise WebLoomError(f"{tool}: {resp['error']}")
        result = resp.get("result", {})
        # MCP tool responses: {content: [{type, text}, ...]}
        content = result.get("content", [])
        if isinstance(content, list) and content:
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(texts)
        return json.dumps(result)

    def call_json(self, tool: str, **args: Any) -> Any:
        """Like call() but tries to parse the response as JSON. Many engine
        tools return JSON-encoded strings — this saves a json.loads."""
        text = self.call(tool, **args)
        try:
            return json.loads(text)
        except Exception:
            return text

    # ── Sugar: shortcut methods for the most common tools ─────────────────
    # All of these just forward to self.call(tool_name, **args). Use call()
    # for any tool not surfaced here.

    def launch_session(self, **args): return self.call("launch_session", **args)
    def list_tabs(self, **args): return self.call("list_tabs", **args)
    def new_tab(self, url: str, **args): return self.call("new_tab", url=url, **args)
    def close_tab(self, **args): return self.call("close_tab", **args)
    def navigate(self, url: str, **args): return self.call("navigate", url=url, **args)
    def scan_tab(self, **args): return self.call("scan_tab", **args)
    def read_tab(self, **args): return self.call("read_tab", **args)
    def screenshot(self, **args): return self.call("screenshot", **args)
    def click(self, selector: str, **args): return self.call("click", selector=selector, **args)
    def fill(self, selector: str, value: str, **args): return self.call("fill", selector=selector, value=value, **args)
    def eval_js(self, code: str, **args): return self.call("eval_js", code=code, **args)
    def wait_for(self, selector: str, **args): return self.call("wait_for", selector=selector, **args)
    def scroll_tab(self, **args): return self.call("scroll_tab", **args)
    def key_type(self, text: str, **args): return self.call("key_type", text=text, **args)
    def key_press(self, key: str, **args): return self.call("key_press", key=key, **args)
    def upload_file(self, selector: str, file_path: str, **args): return self.call("upload_file", selector=selector, file_path=file_path, **args)
    def vision_check(self, question: str, **args): return self.call_json("vision_check", question=question, **args)
    def click_at_coords(self, x: float, y: float, **args): return self.call("click_at_coords", x=x, y=y, **args)
    def enable_stealth(self, **args): return self.call("enable_stealth", **args)
    def lexical_set_text(self, container_selector: str, text: str, **args): return self.call("lexical_set_text", container_selector=container_selector, text=text, **args)
    def draftjs_set_text(self, container_selector: str, text: str, **args): return self.call("draftjs_set_text", container_selector=container_selector, text=text, **args)
    def replay_xhr(self, url: str, **args): return self.call("replay_xhr", url=url, **args)
    def inject_on_new_document(self, script: str, **args): return self.call("inject_on_new_document", script=script, **args)
    def run_parallel(self, calls: list, **args): return self.call_json("run_parallel", calls=calls, **args)
    def solve_captcha(self, type: str, site_key: str, **args): return self.call_json("solve_captcha", type=type, site_key=site_key, **args)
    def drift_heal_suggest(self, old_selector: str, descriptor: str, **args): return self.call_json("drift_heal_suggest", old_selector=old_selector, descriptor=descriptor, **args)
    def get_playbook(self, domain: str | None = None, **args):
        if domain:
            args["domain"] = domain
        return self.call("get_playbook", **args)
    def list_threads(self, **args): return self.call("list_threads", **args)
    def install_thread(self, **args): return self.call("install_thread", **args)
