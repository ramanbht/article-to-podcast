#!/usr/bin/env python3
"""Podcast daemon: watches an iCloud Drive inbox for `request-*.json` files,
turns each URL into an episode, exposes them as a private podcast RSS feed.

Inbox layout (iCloud Drive, writable by an iPhone Shortcut):
  ~/Library/Mobile Documents/com~apple~CloudDocs/Podcast/inbox/
    request-{id}.json   { "id": "...", "url": "..." }

Episode layout (local, served via HTTP):
  ~/Library/Application Support/PodcastDaemon/episodes/
    {id}.mp3            audio
    {id}.txt            script
    {id}.meta.json      { id, url, title, durationSec, createdAt, doneAt }
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import config
from extract import fetch_article
from generate_script import generate_script
from synthesize import synthesize
from server import run_server

POLL_SECONDS = 5
STABLE_CHECKS = 2  # number of polls a file must be stable before we process it
ERROR_LOG = config.DATA_DIR / "errors.log"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def log_error(msg: str) -> None:
    log(msg)
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a") as f:
        f.write(f"[{now_iso()}] {msg}\n")


def is_icloud_placeholder(path: Path) -> bool:
    """iCloud shows undownloaded files as `.<name>.icloud`."""
    return path.name.startswith(".") and path.name.endswith(".icloud")


def materialize_placeholder(placeholder: Path) -> Path | None:
    """Trigger iCloud download of a placeholder. Returns the real path once
    materialized (waits up to 30s), or None on timeout."""
    real_name = placeholder.name[1:].removesuffix(".icloud")  # ".x.json.icloud" -> "x.json"
    real_path = placeholder.parent / real_name
    if real_path.exists():
        return real_path
    try:
        subprocess.run(
            ["/usr/bin/brctl", "download", str(placeholder)],
            check=False, timeout=60, capture_output=True,
        )
    except Exception:
        pass
    for _ in range(60):
        if real_path.exists():
            return real_path
        time.sleep(0.5)
    return None


def find_request_files(inbox: Path) -> list[Path]:
    """Return all materialized request-*.json files, pulling iCloud placeholders down first."""
    materialized: list[Path] = []
    for p in inbox.iterdir():
        if is_icloud_placeholder(p):
            real_name = p.name[1:].removesuffix(".icloud")
            if real_name.startswith("request-") and real_name.endswith(".json"):
                got = materialize_placeholder(p)
                if got is not None:
                    materialized.append(got)
            continue
        if p.name.startswith("request-") and p.name.endswith(".json"):
            materialized.append(p)
    return sorted(materialized)


def write_meta(meta_path: Path, **fields) -> None:
    """Atomic meta JSON write."""
    existing: dict = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
        except Exception:
            existing = {}
    existing.update(fields)
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    tmp.replace(meta_path)


def wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    """Encode WAV → MP3 (mono, 64k) via ffmpeg. ~5x smaller, fine for voice."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-ac", "1", "-b:a", "64k",
            str(mp3_path),
        ],
        check=True,
    )


def process_request(request_path: Path) -> None:
    raw = request_path.read_text()
    try:
        payload = json.loads(raw)
    except Exception as e:
        log_error(f"bad JSON in {request_path.name}: {e}")
        request_path.unlink(missing_ok=True)
        return

    url = (payload.get("url") or "").strip()
    req_id = (payload.get("id") or request_path.stem.removeprefix("request-")).strip()
    if not url or not req_id:
        log_error(f"missing url or id in {request_path.name}: {payload!r}")
        request_path.unlink(missing_ok=True)
        return

    log(f"→ {req_id}: {url}")

    meta_path = config.EPISODES_DIR / f"{req_id}.meta.json"
    script_path = config.EPISODES_DIR / f"{req_id}.txt"
    wav_path = config.EPISODES_DIR / f"{req_id}.wav"
    mp3_path = config.EPISODES_DIR / f"{req_id}.mp3"
    created_at = payload.get("createdAt") or now_iso()
    write_meta(
        meta_path,
        id=req_id, url=url, title=None,
        status="fetching", createdAt=created_at,
    )

    try:
        title, body = fetch_article(url)
        log(f"   fetched: {title!r} ({len(body)} chars)")
        write_meta(meta_path, title=title, status="scripting")

        script = generate_script(url, title, body)
        script_path.write_text(script)
        log(f"   scripted: {len(script)} chars")
        write_meta(meta_path, status="synthesizing")

        duration = synthesize(script, wav_path)
        log(f"   synthesized: {duration:.1f}s WAV")

        wav_to_mp3(wav_path, mp3_path)
        wav_path.unlink(missing_ok=True)  # keep only the smaller MP3
        log(f"   encoded → {mp3_path.name}")

        write_meta(
            meta_path,
            status="done",
            durationSec=round(duration, 1),
            audioFile=mp3_path.name,
            scriptFile=script_path.name,
            doneAt=now_iso(),
        )
        log(f"✓ {req_id} done ({duration:.1f}s audio)")
    except Exception as e:
        tb = traceback.format_exc()
        log_error(f"✗ {req_id}: {e}\n{tb}")
        write_meta(meta_path, status="error", error=str(e), errorAt=now_iso())
    finally:
        request_path.unlink(missing_ok=True)


def watch_loop() -> None:
    log(f"watching inbox: {config.INBOX_DIR}")
    log(f"episodes dir : {config.EPISODES_DIR}")

    sizes: dict[Path, list[int]] = {}
    while True:
        try:
            if not config.INBOX_DIR.exists():
                config.INBOX_DIR.mkdir(parents=True, exist_ok=True)

            requests = find_request_files(config.INBOX_DIR)
            for req in requests:
                try:
                    sz = req.stat().st_size
                except FileNotFoundError:
                    continue
                history = sizes.setdefault(req, [])
                history.append(sz)
                if len(history) < STABLE_CHECKS or len(set(history[-STABLE_CHECKS:])) != 1:
                    continue
                sizes.pop(req, None)
                process_request(req)
        except Exception as e:
            log_error(f"loop error: {e}\n{traceback.format_exc()}")

        time.sleep(POLL_SECONDS)


def main() -> int:
    config.ensure_dirs()
    # Run HTTP server in a daemon thread so the watcher keeps the process alive.
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    try:
        watch_loop()
    except KeyboardInterrupt:
        log("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
