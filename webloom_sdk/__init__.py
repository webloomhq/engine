"""
WebLoom SDK — call the engine from non-MCP Python (CI, cron, scripts, servers).

The MCP server exposes 70+ browser-control tools over JSON-RPC. This SDK is
a thin client that speaks the same protocol over stdio so any Python program
can drive the engine without an MCP host.

Usage:
    from webloom_sdk import Session

    with Session("slot-1") as s:
        s.launch_session()
        s.new_tab("https://kdp.amazon.com")
        s.click('[data-testid="login"]')
        s.fill('#email', "you@example.com")
        # ... any of the engine's 70+ tools

The Session context manager spawns server.py as a child process, sends
JSON-RPC over stdio, and tears down cleanly on exit.
"""

from .client import Session, WebLoomError

__all__ = ["Session", "WebLoomError"]
__version__ = "0.1.0"
