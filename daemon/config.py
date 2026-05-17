"""Shared paths and constants for the daemon and HTTP server."""
from __future__ import annotations

import os
import socket
from pathlib import Path

# Inbox lives in plain iCloud Drive so an iPhone Shortcut can write here
# with no special container or signing. The folder is visible in Files app
# as "iCloud Drive → Podcast".
INBOX_DIR = Path(
    os.environ.get(
        "PODCAST_INBOX",
        str(Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Podcast" / "inbox"),
    )
).expanduser()

# Episodes + metadata are local-only. They're served over HTTP, no need to
# round-trip through iCloud (saves space and sync churn).
DATA_DIR = Path(
    os.environ.get(
        "PODCAST_DATA_DIR",
        str(Path.home() / "Library" / "Application Support" / "PodcastDaemon"),
    )
).expanduser()

EPISODES_DIR = DATA_DIR / "episodes"

# HTTP server
HTTP_HOST = os.environ.get("PODCAST_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("PODCAST_HTTP_PORT", "8765"))

# The base URL the iPhone will hit. With Tailscale, use your Tailnet hostname,
# e.g. http://my-mac.tail-xxxx.ts.net:8765
# Falls back to your local hostname so subscribing on the home LAN works too.
PUBLIC_BASE_URL = os.environ.get("PODCAST_PUBLIC_URL") or f"http://{socket.gethostname()}:{HTTP_PORT}"

# Feed metadata
FEED_TITLE = os.environ.get("PODCAST_FEED_TITLE", "My Article Podcast")
FEED_DESCRIPTION = os.environ.get(
    "PODCAST_FEED_DESCRIPTION",
    "Articles I saved to listen to, narrated.",
)
FEED_AUTHOR = os.environ.get("PODCAST_FEED_AUTHOR", "Me")


def ensure_dirs() -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
