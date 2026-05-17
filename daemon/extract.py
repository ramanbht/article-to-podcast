"""Fetch a URL and extract the clean article body + title."""
from __future__ import annotations

import trafilatura


def fetch_article(url: str) -> tuple[str, str]:
    """Return (title, body_text). Raises RuntimeError on failure."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise RuntimeError(f"failed to fetch {url}")

    body = trafilatura.extract(
        downloaded,
        favor_recall=True,
        include_comments=False,
        include_tables=False,
        deduplicate=True,
    )
    if not body or len(body.strip()) < 200:
        raise RuntimeError(f"extracted body too short from {url} ({len(body or '')} chars)")

    meta = trafilatura.extract_metadata(downloaded)
    title = (meta.title if meta and meta.title else url).strip()

    return title, body.strip()
