"""Native macOS notifications (via osascript — no dependency, no install).

Used to alert the user to conditions that need a human action the daemon
can't take itself — chiefly an expired `claude` login, which silently halts
all episode generation until the user runs `claude /login`.
"""
from __future__ import annotations

import subprocess


def _osa_quote(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_notification(title: str, message: str, subtitle: str | None = None,
                       sound: str = "Basso") -> None:
    """Post a Notification Center banner. Best-effort; never raises."""
    script = f"display notification {_osa_quote(message)} with title {_osa_quote(title)}"
    if subtitle:
        script += f" subtitle {_osa_quote(subtitle)}"
    if sound:
        script += f" sound name {_osa_quote(sound)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=10)
    except Exception:
        pass  # a failed notification must never break the pipeline
