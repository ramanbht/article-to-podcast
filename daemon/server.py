"""HTTP server: exposes the podcast RSS feed and serves MP3 episodes.

Endpoints:
  GET  /                  → simple HTML index (recent episodes, errors, submit form)
  GET  /feed.xml          → RSS 2.0 feed for Apple Podcasts
  GET  /episodes/<file>   → serve an episode file (MP3, txt, etc.)
  POST /submit            → enqueue a URL (form-encoded or JSON: { "url": "..." })
"""
from __future__ import annotations

import html
import json
import mimetypes
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config


def _load_meta() -> list[dict]:
    """Return all episode metadata, newest first."""
    items: list[dict] = []
    for p in sorted(config.EPISODES_DIR.glob("*.meta.json")):
        try:
            items.append(json.loads(p.read_text()))
        except Exception:
            continue
    items.sort(key=lambda m: m.get("doneAt") or m.get("createdAt") or "", reverse=True)
    return items


def _iso_to_rfc2822(iso: str | None) -> str:
    if not iso:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def _build_feed() -> bytes:
    base = config.PUBLIC_BASE_URL.rstrip("/")
    chan_title = html.escape(config.FEED_TITLE)
    chan_desc = html.escape(config.FEED_DESCRIPTION)
    chan_author = html.escape(config.FEED_AUTHOR)
    chan_link = base + "/"

    items_xml: list[str] = []
    for m in _load_meta():
        if m.get("status") != "done":
            continue
        mp3_name = m.get("audioFile") or f"{m['id']}.mp3"
        mp3_path = config.EPISODES_DIR / mp3_name
        if not mp3_path.exists():
            continue

        title = html.escape((m.get("title") or m.get("url") or "Episode").strip())
        url = html.escape(m.get("url") or "")
        description = html.escape(f"{m.get('url') or ''}")
        guid = html.escape(str(m["id"]))
        pub = _iso_to_rfc2822(m.get("doneAt") or m.get("createdAt"))
        duration = int(round(float(m.get("durationSec") or 0)))
        length = mp3_path.stat().st_size
        enclosure_url = f"{base}/episodes/{mp3_name}"

        items_xml.append(f"""    <item>
      <title>{title}</title>
      <description>{description}</description>
      <link>{url}</link>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="{html.escape(enclosure_url)}" length="{length}" type="audio/mpeg" />
      <itunes:duration>{duration}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{chan_title}</title>
    <link>{chan_link}</link>
    <description>{chan_desc}</description>
    <language>en-us</language>
    <itunes:author>{chan_author}</itunes:author>
    <itunes:summary>{chan_desc}</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="News"/>
{chr(10).join(items_xml)}
  </channel>
</rss>
"""
    return feed.encode("utf-8")


def _build_index() -> bytes:
    items = _load_meta()
    done = [m for m in items if m.get("status") == "done"]
    in_flight = [m for m in items if m.get("status") not in ("done", "error")]
    errored = [m for m in items if m.get("status") == "error"]

    def li(m: dict, show_status: bool = False) -> str:
        title = html.escape(m.get("title") or m.get("url") or m["id"])
        url = html.escape(m.get("url") or "")
        dur = m.get("durationSec")
        meta_bits = []
        if dur:
            mm, ss = divmod(int(dur), 60)
            meta_bits.append(f"{mm}:{ss:02d}")
        if show_status:
            meta_bits.append(html.escape(m.get("status", "?")))
            if m.get("error"):
                meta_bits.append(html.escape(m["error"][:120]))
        meta = " · ".join(meta_bits)
        href = f"/episodes/{html.escape(m.get('audioFile') or '')}" if m.get("audioFile") else url
        return f'<li><a href="{href}">{title}</a><br><small>{url} · {meta}</small></li>'

    feed_url = html.escape(config.PUBLIC_BASE_URL.rstrip("/") + "/feed.xml")
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(config.FEED_TITLE)}</title>
<style>
  body {{ font: 15px -apple-system, system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; color: #222; }}
  h1 {{ margin-bottom: 4px; }}
  h2 {{ margin-top: 32px; font-size: 16px; color: #666; text-transform: uppercase; letter-spacing: 0.05em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 12px 0; border-bottom: 1px solid #eee; }}
  small {{ color: #888; }}
  a {{ color: #2178cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  input[type=text] {{ width: 100%; padding: 10px; font-size: 15px; border: 1px solid #ccc; border-radius: 6px; }}
  button {{ margin-top: 8px; padding: 8px 16px; font-size: 14px; }}
  code {{ background: #f4f4f4; padding: 2px 4px; border-radius: 3px; }}
</style></head>
<body>
<h1>{html.escape(config.FEED_TITLE)}</h1>
<p><small>Subscribe in Apple Podcasts: <code>{feed_url}</code></small></p>

<form method="post" action="/submit">
  <input type="text" name="url" placeholder="Paste article URL" autocomplete="off" autofocus>
  <button type="submit">Add</button>
</form>

{'<h2>In progress</h2><ul>' + ''.join(li(m, True) for m in in_flight) + '</ul>' if in_flight else ''}

<h2>Episodes ({len(done)})</h2>
<ul>
{''.join(li(m) for m in done) or '<li><small>nothing yet</small></li>'}
</ul>

{'<h2>Errors</h2><ul>' + ''.join(li(m, True) for m in errored) + '</ul>' if errored else ''}
</body></html>
"""
    return body.encode("utf-8")


def _enqueue(url: str) -> str:
    """Write a request file to the inbox. Returns the id."""
    rid = uuid.uuid4().hex[:12]
    payload = {
        "id": rid,
        "url": url.strip(),
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    target = config.INBOX_DIR / f"request-{rid}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(target)
    return rid


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "PodcastDaemon/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # Quieter access logging.
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, head_only: bool = False) -> None:
        if not path.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/episodes/"):
            name = path[len("/episodes/"):]
            if not _SAFE_NAME.match(name):
                return self._send(400, b"", "text/plain")
            return self._send_file(config.EPISODES_DIR / name, head_only=True)
        # For non-file routes, just answer 200 with no body.
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._send(200, _build_index(), "text/html; charset=utf-8")
        if path == "/feed.xml":
            return self._send(200, _build_feed(), "application/rss+xml; charset=utf-8")
        if path.startswith("/episodes/"):
            name = path[len("/episodes/"):]
            if not _SAFE_NAME.match(name):
                return self._send(400, b"bad name", "text/plain")
            return self._send_file(config.EPISODES_DIR / name)
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/submit":
            return self._send(404, b"not found", "text/plain")

        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        ctype = (self.headers.get("Content-Type") or "").lower()

        url = ""
        if "application/json" in ctype:
            try:
                payload = json.loads(raw or "{}")
                url = str(payload.get("url") or "").strip()
            except Exception:
                pass
        else:
            qs = parse_qs(raw)
            url = (qs.get("url", [""])[0]).strip()

        if not url:
            return self._send(400, b"missing url", "text/plain")

        rid = _enqueue(url)

        if "application/json" in ctype:
            return self._send(
                200,
                json.dumps({"id": rid, "url": url, "status": "queued"}).encode(),
                "application/json",
            )
        # form: redirect back to index
        self._send(303, b"", "text/plain", extra={"Location": "/"})


def run_server() -> None:
    config.ensure_dirs()
    address = (config.HTTP_HOST, config.HTTP_PORT)
    httpd = ThreadingHTTPServer(address, Handler)
    print(f"[http] listening on http://{config.HTTP_HOST}:{config.HTTP_PORT}", flush=True)
    print(f"[http] public base: {config.PUBLIC_BASE_URL}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    run_server()
