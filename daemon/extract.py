"""Fetch a URL and extract the clean article body + title.

Routes to a site-specific handler for well-known JS-required sites
(Reddit via its public .json endpoint), otherwise falls back to trafilatura.
Detects obvious "error pages" (JS-required stubs, paywalls, login walls) and
fails early before wasting a Claude call on them.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

import trafilatura

USER_AGENT = "podcast-daemon/0.2 (https://github.com/ramanbht/article-to-podcast)"
MIN_BODY_CHARS = 200

# Bodies/titles that match these patterns almost certainly came from a JS-
# required stub, paywall, or login wall — don't bother asking Claude.
_ERROR_PAGE_PATTERNS = [
    re.compile(r"\bjavascript is not available\b", re.I),
    re.compile(r"\bplease enable javascript\b", re.I),
    re.compile(r"\benable javascript and cookies\b", re.I),
    re.compile(r"\bjust a moment\b", re.I),  # Cloudflare interstitial
    re.compile(r"\bare you a human\b", re.I),
    re.compile(r"\bsign in to continue\b", re.I),
    re.compile(r"\blogin required\b", re.I),
    re.compile(r"\baccess denied\b", re.I),
    re.compile(r"\b403 forbidden\b", re.I),
    re.compile(r"\b404 not found\b", re.I),
    re.compile(r"\byour browser is not supported\b", re.I),
]


class UnscriptableError(RuntimeError):
    """The fetched page is something we shouldn't ask Claude to write about
    (error page, paywall, login wall, etc.). Daemon handles cleanly."""


# ---------------------------------------------------------------------------
# Reddit special-case (public .json endpoint, no auth required)
# ---------------------------------------------------------------------------

_REDDIT_HOST = re.compile(r"^(?:www\.|old\.|m\.)?reddit\.com$", re.I)


def _is_reddit(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return bool(_REDDIT_HOST.match(host))
    except Exception:
        return False


def _http_get(url: str, *, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _resolve_redirects(url: str) -> str:
    """Follow redirects to canonical URL. Used for Reddit /s/ short links."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.geturl() or url
    except (HTTPError, URLError):
        # Some servers don't allow HEAD — try a GET that we immediately close.
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.geturl() or url
        except Exception:
            return url


def _reddit_json_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if not path.endswith(".json"):
        path += ".json"
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, path, "", parsed.query, "")
    )


def _fetch_reddit(url: str) -> tuple[str, str]:
    # Resolve /s/<token> short links to the canonical /r/sub/comments/... URL.
    canonical = _resolve_redirects(url)
    json_url = _reddit_json_url(canonical)
    raw = _http_get(json_url)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"reddit returned non-JSON: {e}") from e

    if not isinstance(data, list) or len(data) == 0:
        raise RuntimeError(f"unexpected reddit JSON shape from {json_url}")

    post = data[0]["data"]["children"][0]["data"]
    title = (post.get("title") or url).strip()
    parts: list[str] = []
    if post.get("selftext"):
        parts.append(post["selftext"].strip())
    elif post.get("url_overridden_by_dest") or post.get("url"):
        parts.append(f"[Link post pointing to: {post.get('url_overridden_by_dest') or post['url']}]")

    # Top-level comments, capped.
    if len(data) > 1:
        comments = []
        for c in data[1]["data"].get("children", []):
            if c.get("kind") != "t1":
                continue
            cdata = c["data"]
            body = (cdata.get("body") or "").strip()
            if not body or body == "[deleted]" or body == "[removed]" or len(body) < 60:
                continue
            author = cdata.get("author") or "?"
            score = cdata.get("score") or 0
            comments.append(f"— {author} ({score} pts):\n{body}")
            if len(comments) >= 15:
                break
        if comments:
            parts.append("\n\nDiscussion:\n\n" + "\n\n".join(comments))

    body = "\n\n".join(parts).strip()
    if len(body) < MIN_BODY_CHARS:
        raise UnscriptableError(
            f"reddit post had insufficient content for a podcast ({len(body)} chars)"
        )
    return title, body


# ---------------------------------------------------------------------------
# Generic (trafilatura) path
# ---------------------------------------------------------------------------

def _fetch_generic(url: str) -> tuple[str, str]:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise RuntimeError(f"failed to fetch {url}")

    body = trafilatura.extract(
        downloaded,
        favor_recall=True,
        include_comments=False,
        include_tables=False,
        deduplicate=True,
    ) or ""

    meta = trafilatura.extract_metadata(downloaded)
    title = (meta.title if meta and meta.title else url).strip()

    # Sniff for obvious error/paywall stubs even if body length passes.
    sample = (title + "\n" + body[:1500])
    for pat in _ERROR_PAGE_PATTERNS:
        if pat.search(sample):
            raise UnscriptableError(
                f"page looks like an error/login/paywall stub: matched /{pat.pattern}/ on {url}"
            )

    if len(body.strip()) < MIN_BODY_CHARS:
        raise UnscriptableError(
            f"extracted body too short from {url} ({len(body.strip())} chars) — "
            f"likely JS-required, paywalled, or login-walled"
        )

    return title, body.strip()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def fetch_article(url: str) -> tuple[str, str]:
    """Return (title, body_text). Raises UnscriptableError for known-unscriptable
    sources (so the daemon can log cleanly without invoking Claude)."""
    if _is_reddit(url):
        return _fetch_reddit(url)
    return _fetch_generic(url)
