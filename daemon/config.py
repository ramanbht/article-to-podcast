"""Shared paths and constants for the daemon and HTTP server."""
from __future__ import annotations

import os
import socket
from pathlib import Path

# ---------------------------------------------------------------------------
# Inbox: where iPhone Shortcuts drop request files (via iCloud Drive).
# ---------------------------------------------------------------------------
INBOX_DIR = Path(
    os.environ.get(
        "PODCAST_INBOX",
        str(Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Podcast" / "inbox"),
    )
).expanduser()

# ---------------------------------------------------------------------------
# Local data: generated episodes + knowledge vault + state.
# ---------------------------------------------------------------------------
DATA_DIR = Path(
    os.environ.get(
        "PODCAST_DATA_DIR",
        str(Path.home() / "Library" / "Application Support" / "PodcastDaemon"),
    )
).expanduser()

EPISODES_DIR = DATA_DIR / "episodes"

# Knowledge vault: one markdown note per article. Obsidian/Logseq-compatible.
VAULT_DIR = Path(
    os.environ.get("PODCAST_VAULT_DIR", str(DATA_DIR / "vault"))
).expanduser()

# ---------------------------------------------------------------------------
# HTTP server (still used for the local landing page / submit form).
# ---------------------------------------------------------------------------
HTTP_HOST = os.environ.get("PODCAST_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("PODCAST_HTTP_PORT", "8765"))

# Public base URL for `<enclosure>` and feed links. When B2 is configured,
# this should be the B2 public URL pattern, e.g.
#   https://f005.backblazeb2.com/file/my-podcast
# Otherwise falls back to the local mDNS hostname.
PUBLIC_BASE_URL = os.environ.get("PODCAST_PUBLIC_URL") or f"http://{socket.gethostname()}:{HTTP_PORT}"

# ---------------------------------------------------------------------------
# Feed metadata.
# ---------------------------------------------------------------------------
FEED_TITLE = os.environ.get("PODCAST_FEED_TITLE", "My Article Podcast")
FEED_DESCRIPTION = os.environ.get(
    "PODCAST_FEED_DESCRIPTION",
    "Articles I saved to listen to, narrated.",
)
FEED_AUTHOR = os.environ.get("PODCAST_FEED_AUTHOR", "Me")

# The channel <link> in RSS — the "website for this show". Defaults to the
# feed URL itself if not set (always reachable). Override to your GitHub repo
# / personal site / anything that returns 200.
FEED_WEBSITE = os.environ.get("PODCAST_FEED_WEBSITE", "").strip()

# Bucket key for the cover image. Apple Podcasts caches artwork by URL very
# aggressively — change this filename whenever you publish a new cover so Apple
# refetches instead of showing the old one for days.
COVER_KEY = os.environ.get("PODCAST_COVER_KEY", "cover.jpg").strip()

# ---------------------------------------------------------------------------
# Backblaze B2 (optional). When all three are set, the daemon uploads each
# completed MP3 + feed.xml to the bucket and the feed URLs point at B2 (which
# gives you HTTPS + reachability from anywhere — so Apple Podcasts works).
# ---------------------------------------------------------------------------
B2_KEY_ID = os.environ.get("PODCAST_B2_KEY_ID", "").strip()
B2_APP_KEY = os.environ.get("PODCAST_B2_APP_KEY", "").strip()
B2_BUCKET = os.environ.get("PODCAST_B2_BUCKET", "").strip()

# Path prefix inside the bucket. Lets you share one bucket across projects.
B2_PREFIX = os.environ.get("PODCAST_B2_PREFIX", "").strip().strip("/")


def b2_enabled() -> bool:
    return bool(B2_KEY_ID and B2_APP_KEY and B2_BUCKET)


# ---------------------------------------------------------------------------
# Script generation knobs.
# ---------------------------------------------------------------------------
SCRIPT_MODEL = os.environ.get("PODCAST_SCRIPT_MODEL", "claude-opus-4-7")
GROUPING_MODEL = os.environ.get("PODCAST_GROUPING_MODEL", "claude-haiku-4-5-20251001")


def ensure_dirs() -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
