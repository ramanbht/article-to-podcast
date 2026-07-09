#!/usr/bin/env python3
"""Podcast daemon: watches an iCloud Drive inbox for request files, batches
them, groups related ones, generates memory-aware scripts, synthesizes audio,
optionally uploads to Backblaze B2, and maintains a markdown knowledge vault.

Per-iteration flow:
  1. Find all stable request files in the inbox.
  2. Fetch each article's body.
  3. Load vault and build the memory context.
  4. Ask Claude to group articles that should be combined.
  5. For each group, generate script + metadata, synthesize, encode to MP3.
  6. Save a vault note. If B2 is enabled, upload MP3 + new feed.xml.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import combine
import config
import notify
import upload
import vault
from extract import UnscriptableError, fetch_article
from generate_script import ClaudeDeclinedError, RetriableError, generate_script
from memory import build_context, suggest_related_slugs
from server import build_feed, run_server
from synthesize import synthesize

POLL_SECONDS = 5
STABLE_CHECKS = 2  # number of polls a file must be stable before we process it
ERROR_LOG = config.DATA_DIR / "errors.log"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def log_error(msg: str) -> None:
    log(msg)
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a") as f:
        f.write(f"[{now_iso()}] {msg}\n")


# ---------------------------------------------------------------------------
# iCloud placeholder handling
#
# iCloud evicts file *contents* to the cloud in two visible forms:
#   1. `.{name}.icloud` sidecar placeholders (classic, when file not yet
#      downloaded at all).
#   2. "dataless" files that keep their real name but have the SF_DATALESS
#      flag set (macOS "Optimize Mac Storage" evicted the body). Reading one
#      directly can raise OSError EDEADLK because the OS won't auto-download
#      mid-read. These look like normal files, so we must check the flag.
# Both are resolved by `brctl download <path>`.
# ---------------------------------------------------------------------------

SF_DATALESS = 0x40000000  # macOS <sys/stat.h>


def is_icloud_placeholder(path: Path) -> bool:
    return path.name.startswith(".") and path.name.endswith(".icloud")


def is_dataless(path: Path) -> bool:
    """True if the file is an iCloud dataless placeholder (contents evicted)."""
    try:
        return bool(os.stat(path).st_flags & SF_DATALESS)
    except (AttributeError, OSError):
        return False


def _brctl_download(path: Path) -> None:
    try:
        subprocess.run(
            ["/usr/bin/brctl", "download", str(path)],
            check=False, timeout=60, capture_output=True,
        )
    except Exception:
        pass


def ensure_downloaded(path: Path, timeout_s: float = 30.0) -> bool:
    """If `path` is a dataless placeholder, trigger its download and wait for
    the contents to materialize. Returns True if the file is (now) materialized."""
    if not is_dataless(path):
        return True
    _brctl_download(path)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not is_dataless(path):
            return True
        time.sleep(0.5)
    return not is_dataless(path)


def materialize_placeholder(placeholder: Path) -> Path | None:
    real_name = placeholder.name[1:].removesuffix(".icloud")
    real_path = placeholder.parent / real_name
    if real_path.exists():
        return real_path
    _brctl_download(placeholder)
    for _ in range(60):
        if real_path.exists():
            return real_path
        time.sleep(0.5)
    return None


REQUEST_SUFFIXES = (".json", ".txt")  # iOS Shortcuts defaults to .txt when
                                     # writing Text content — accept both.

# Retriable-failure backoff: when script-gen fails transiently (expired login,
# rate limit), keep the request file but don't hammer it every poll. Retry at
# most once per DEFER_COOLDOWN_SEC.
DEFER_COOLDOWN_SEC = 300
_deferred_until: dict[Path, float] = {}
_deferred_reason: dict[Path, str] = {}

# Auth-down notification state: notify once when auth goes down, once when it
# recovers — never per-retry.
_AUTH_MARKERS = ("authenticat", "401", "please run /login", "not logged in")
_auth_down_notified = False


def _looks_like_auth(reason: str) -> bool:
    low = (reason or "").lower()
    return any(m in low for m in _AUTH_MARKERS)


def _note_auth_down(reason: str) -> None:
    """Post a one-time notification that the claude login has expired."""
    global _auth_down_notified
    if _auth_down_notified or not _looks_like_auth(reason):
        return
    _auth_down_notified = True
    log_error("🔔 notifying: claude login appears to be down")
    notify.macos_notification(
        title="RamanCast paused",
        subtitle="Claude login expired",
        message="New episodes are queued. Run  claude /login  in Terminal to resume.",
    )


def _note_auth_recovered() -> None:
    """If we'd flagged auth as down, announce recovery once."""
    global _auth_down_notified
    if not _auth_down_notified:
        return
    _auth_down_notified = False
    log("🔔 notifying: claude login recovered")
    notify.macos_notification(
        title="RamanCast resumed",
        subtitle="Claude login working again",
        message="Queued articles are being turned into episodes.",
        sound="Glass",
    )


def _defer_group(group: list[dict], reason: str) -> None:
    due = time.time() + DEFER_COOLDOWN_SEC
    for a in group:
        p = a["_path"]
        _deferred_until[p] = due
        # Log once per new reason so a persistent auth outage doesn't spam.
        if _deferred_reason.get(p) != reason:
            _deferred_reason[p] = reason
            log_error(
                f"↺ {p.name} deferred (will retry after ~{DEFER_COOLDOWN_SEC//60}m): {reason}"
            )
    _note_auth_down(reason)


def _is_deferred(path: Path) -> bool:
    due = _deferred_until.get(path)
    if due is None:
        return False
    if time.time() >= due:
        # Cooldown elapsed — clear and allow a retry.
        _deferred_until.pop(path, None)
        return False
    return True


def _is_request_name(name: str) -> bool:
    return name.startswith("request-") and name.endswith(REQUEST_SUFFIXES)


def find_request_files(inbox: Path) -> list[Path]:
    materialized: list[Path] = []
    for p in inbox.iterdir():
        if is_icloud_placeholder(p):
            real_name = p.name[1:].removesuffix(".icloud")
            if _is_request_name(real_name):
                got = materialize_placeholder(p)
                if got is not None:
                    materialized.append(got)
            continue
        if _is_request_name(p.name):
            materialized.append(p)
    return sorted(materialized)


# ---------------------------------------------------------------------------
# Meta JSON helpers (per-episode, in EPISODES_DIR)
# ---------------------------------------------------------------------------

def write_meta(meta_path: Path, **fields) -> None:
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


# ---------------------------------------------------------------------------
# WAV → MP3
# ---------------------------------------------------------------------------

def wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-ac", "1", "-b:a", "64k",
            str(mp3_path),
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# B2 upload (optional)
# ---------------------------------------------------------------------------

def upload_episode_and_feed(mp3_path: Path) -> None:
    """Upload MP3 then regenerate-and-upload feed.xml."""
    if not config.b2_enabled():
        return
    try:
        upload.upload_file(mp3_path, f"episodes/{mp3_path.name}", content_type="audio/mpeg")
        log(f"   ↑ B2: episodes/{mp3_path.name}")
        feed_bytes = build_feed()
        # Write feed locally too so the / index reflects it.
        local_feed = config.DATA_DIR / "feed.xml"
        local_feed.write_bytes(feed_bytes)
        # Upload feed last so subscribers never see a feed referencing a missing MP3.
        upload.upload_file(local_feed, "feed.xml", content_type="application/rss+xml")
        log(f"   ↑ B2: feed.xml")
    except Exception as e:
        log_error(f"B2 upload failed: {e}")


# ---------------------------------------------------------------------------
# Request → episode
# ---------------------------------------------------------------------------

# Per-file noisy-error suppression so iCloud lock contention doesn't spam
# the log every poll cycle. Keyed by path → last error message we logged.
_request_read_errors: dict[Path, str] = {}


def _read_request_text(req_path: Path) -> str:
    """Read a request file, first materializing it if iCloud evicted its
    contents (dataless placeholder), then retrying transient lock errors
    (EDEADLK/EAGAIN) with short backoff. Raises OSError if all retries fail;
    the caller logs once-per-error-state and leaves the file for next poll."""
    ensure_downloaded(req_path)
    last: Exception | None = None
    for delay in (0, 0.2, 0.8, 2.0):
        if delay:
            time.sleep(delay)
        try:
            return req_path.read_text()
        except OSError as e:
            last = e
            # 11 = EDEADLK / EAGAIN on iCloud-managed files
            if e.errno not in (11, 16, 35):
                raise
            # A dataless file can throw EDEADLK on read — (re)trigger download.
            ensure_downloaded(req_path)
    assert last is not None
    raise last


def _parse_request(req_path: Path) -> dict | None:
    try:
        text = _read_request_text(req_path)
    except OSError as e:
        # Transient I/O — don't delete the file, just try again next poll.
        # Log only when the error changes (avoid 6000-line log spam).
        msg = f"{type(e).__name__} errno={e.errno}: {e}"
        if _request_read_errors.get(req_path) != msg:
            _request_read_errors[req_path] = msg
            log_error(f"⏳ {req_path.name}: i/o not ready ({msg}) — will retry")
        return None

    # Successful read — clear any stale failure state.
    _request_read_errors.pop(req_path, None)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        log_error(f"✗ {req_path.name}: malformed JSON: {e}")
        try:
            req_path.unlink()
        except Exception:
            pass
        return None

    url = (payload.get("url") or "").strip()
    rid = (payload.get("id") or req_path.stem.removeprefix("request-")).strip()
    if not url or not rid:
        log_error(f"✗ {req_path.name}: missing url or id: {payload!r}")
        try:
            req_path.unlink()
        except Exception:
            pass
        return None
    return {
        "id": rid,
        "url": url,
        "createdAt": payload.get("createdAt") or now_iso(),
        "_path": req_path,
    }


def _fetch_with_status(article: dict) -> dict | None:
    """Fetch the article body, writing a per-request meta while we work."""
    meta_path = config.EPISODES_DIR / f"{article['id']}.meta.json"
    write_meta(
        meta_path,
        id=article["id"],
        url=article["url"],
        title=None,
        status="fetching",
        createdAt=article["createdAt"],
    )
    try:
        title, body = fetch_article(article["url"])
    except UnscriptableError as e:
        log_error(f"⊘ {article['id']} skipped: {e}")
        write_meta(meta_path, status="skipped", error=str(e), skippedAt=now_iso())
        article["_path"].unlink(missing_ok=True)
        return None
    except Exception as e:
        log_error(f"✗ fetch failed for {article['id']} ({article['url']}): {e}")
        write_meta(meta_path, status="error", error=f"fetch: {e}", errorAt=now_iso())
        article["_path"].unlink(missing_ok=True)
        return None
    article["title"] = title
    article["body"] = body
    write_meta(meta_path, title=title, status="queued")
    log(f"   fetched: {title!r} ({len(body)} chars) [{article['id']}]")
    return article


def _episode_id(group: list[dict]) -> str:
    """Pick a stable id for the episode produced from a group."""
    if len(group) == 1:
        return group[0]["id"]
    # Use first id as the canonical episode id; combined episodes mark "combined_with".
    return group[0]["id"]


def process_group(group: list[dict], memory_context: str, vault_notes: list[vault.Note]) -> None:
    ep_id = _episode_id(group)
    title_for_log = group[0]["title"] if len(group) == 1 else f"{len(group)} combined articles starting with {group[0]['title']!r}"
    log(f"→ episode {ep_id}: {title_for_log}")

    meta_path = config.EPISODES_DIR / f"{ep_id}.meta.json"
    script_path = config.EPISODES_DIR / f"{ep_id}.txt"
    wav_path = config.EPISODES_DIR / f"{ep_id}.wav"
    mp3_path = config.EPISODES_DIR / f"{ep_id}.mp3"

    combined_ids = [a["id"] for a in group]
    write_meta(
        meta_path,
        id=ep_id, url=group[0]["url"],
        title=group[0]["title"],
        status="scripting",
        combinedIds=combined_ids if len(group) > 1 else None,
    )

    try:
        script_result = generate_script(
            [{"url": a["url"], "title": a["title"], "body": a["body"]} for a in group],
            memory_context=memory_context,
        )
        script_path.write_text(script_result.script)
        log(f"   scripted: {len(script_result.script)} chars; topics={script_result.topics}")
        write_meta(meta_path, status="synthesizing", topics=script_result.topics)

        duration = synthesize(script_result.script, wav_path)
        log(f"   synthesized: {duration:.1f}s WAV")

        wav_to_mp3(wav_path, mp3_path)
        wav_path.unlink(missing_ok=True)
        log(f"   encoded → {mp3_path.name}")

        write_meta(
            meta_path,
            status="done",
            durationSec=round(duration, 1),
            audioFile=mp3_path.name,
            scriptFile=script_path.name,
            doneAt=now_iso(),
        )

        # Write vault note.
        date_str = vault.today_str()
        episode_title = group[0]["title"] or f"episode {ep_id}"
        if len(group) > 1:
            episode_title = " + ".join(a["title"] for a in group)
        slug = vault.note_slug(date_str, episode_title)
        related = suggest_related_slugs(vault_notes, script_result.topics)
        vault.save_note(
            slug=slug,
            url=group[0]["url"] if len(group) == 1 else "; ".join(a["url"] for a in group),
            title=episode_title,
            date=date_str,
            duration_sec=duration,
            episode_id=ep_id,
            topics=script_result.topics,
            summary=script_result.summary,
            key_claims=script_result.key_claims,
            related_slugs=related,
            extra={"combined_ids": combined_ids} if len(group) > 1 else None,
        )
        log(f"   📝 vault: {slug}.md ({len(script_result.key_claims)} claims)")

        # Upload to B2 (no-op if not configured).
        upload_episode_and_feed(mp3_path)

        # A successful script means auth is healthy again — notify if we'd
        # previously flagged it as down.
        _note_auth_recovered()

        log(f"✓ {ep_id} done ({duration:.1f}s audio)")
    except RetriableError as e:
        # Transient, fixable failure (expired `claude` login, rate limit,
        # network blip). Do NOT consume the request — leave it in the inbox
        # and back off so we retry automatically once the problem clears.
        _defer_group(group, str(e))
        write_meta(meta_path, status="deferred", error=str(e), deferredAt=now_iso())
        return  # skip the unlink in finally
    except (UnscriptableError, ClaudeDeclinedError) as e:
        # Expected permanent failure (junk body, paywall, X stub, etc.).
        log_error(f"⊘ {ep_id} skipped: {e}")
        write_meta(meta_path, status="skipped", error=str(e), skippedAt=now_iso())
    except Exception as e:
        tb = traceback.format_exc()
        log_error(f"✗ {ep_id}: {e}\n{tb}")
        write_meta(meta_path, status="error", error=str(e), errorAt=now_iso())
    # Consume request files for the group on success or permanent failure.
    # (RetriableError returns early above, keeping the files for a later retry.)
    for a in group:
        a["_path"].unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def watch_loop() -> None:
    log(f"watching inbox: {config.INBOX_DIR}")
    log(f"episodes dir : {config.EPISODES_DIR}")
    log(f"vault dir    : {config.VAULT_DIR}")
    log(f"B2 upload    : {'enabled (' + config.B2_BUCKET + ')' if config.b2_enabled() else 'disabled'}")

    sizes: dict[Path, list[int]] = {}
    while True:
        try:
            if not config.INBOX_DIR.exists():
                config.INBOX_DIR.mkdir(parents=True, exist_ok=True)

            requests = find_request_files(config.INBOX_DIR)

            # Stability check: only process files that haven't changed size for
            # STABLE_CHECKS consecutive polls (iCloud sync still in flight).
            stable: list[Path] = []
            for req in requests:
                if _is_deferred(req):
                    continue  # in retry backoff after a transient failure
                try:
                    sz = req.stat().st_size
                except FileNotFoundError:
                    continue
                history = sizes.setdefault(req, [])
                history.append(sz)
                if len(history) >= STABLE_CHECKS and len(set(history[-STABLE_CHECKS:])) == 1:
                    stable.append(req)
                    sizes.pop(req, None)

            if stable:
                _run_batch(stable)
        except Exception as e:
            log_error(f"loop error: {e}\n{traceback.format_exc()}")

        time.sleep(POLL_SECONDS)


def _dedupe_by_url(parsed: list[dict]) -> list[dict]:
    """Drop duplicate URLs within a batch (e.g. an accidental double-submit).
    Keeps the first occurrence; removes the request files of the dropped ones."""
    seen: set[str] = set()
    out: list[dict] = []
    for a in parsed:
        key = a["url"].strip().rstrip("/")
        if key in seen:
            log(f"   dedupe: dropping duplicate submission of {a['url']} [{a['id']}]")
            a["_path"].unlink(missing_ok=True)
            continue
        seen.add(key)
        out.append(a)
    return out


def _run_batch(request_paths: list[Path]) -> None:
    # Parse + dedupe + fetch all requests in this batch.
    parsed = [p for p in (_parse_request(rp) for rp in request_paths) if p is not None]
    parsed = _dedupe_by_url(parsed)
    fetched = [a for a in (_fetch_with_status(a) for a in parsed) if a is not None]
    if not fetched:
        return

    vault_notes = vault.load_all_notes()
    memory_context = build_context(vault_notes)
    if memory_context:
        log(f"   memory context: {sum(1 for _ in vault_notes)} vault notes loaded")

    # Group related articles (no-op for single-article batches).
    groups = combine.group_articles(fetched)
    if len(groups) != len(fetched):
        log(f"   combine: {len(fetched)} articles → {len(groups)} episode(s)")

    for g in groups:
        process_group(g, memory_context, vault_notes)


def main() -> int:
    config.ensure_dirs()
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    try:
        watch_loop()
    except KeyboardInterrupt:
        log("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
