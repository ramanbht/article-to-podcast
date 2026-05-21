"""Build a context summary from the vault to inform new script generation.

The context is appended to the generate-script prompt and tells Claude what
the listener has already heard, so it can:
  - skip or briefly reference already-covered ideas
  - emphasize topics the listener has shown sustained interest in
  - trim content unrelated to their established interests
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from vault import Note


# Soft caps so the prompt doesn't balloon when the vault gets big.
MAX_TOPICS = 12
MAX_RECENT_NOTES = 30
MAX_CLAIMS_PER_NOTE = 5
MAX_CLAIMS_TOTAL = 80


def topic_counts(notes: Iterable[Note]) -> Counter:
    c: Counter = Counter()
    for n in notes:
        for t in n.topics:
            c[t] += 1
    return c


def build_context(notes: list[Note]) -> str:
    """Produce a prompt-shaped string summarizing the listener's history.

    Returns an empty string if the vault is empty (so callers can decide
    whether to include the LISTENER CONTEXT block at all).
    """
    if not notes:
        return ""

    counts = topic_counts(notes)
    top_topics = counts.most_common(MAX_TOPICS)
    topics_str = ", ".join(f"{t} ({n})" for t, n in top_topics) or "(none yet)"

    # Recency-biased claim list. Sort by date desc, take from each note up to
    # MAX_CLAIMS_PER_NOTE, cap total list.
    recent = sorted(notes, key=lambda n: n.date or "", reverse=True)[:MAX_RECENT_NOTES]
    claim_lines: list[str] = []
    for n in recent:
        for claim in n.key_claims[:MAX_CLAIMS_PER_NOTE]:
            claim_lines.append(f"- {claim} [from: {n.title}]")
            if len(claim_lines) >= MAX_CLAIMS_TOTAL:
                break
        if len(claim_lines) >= MAX_CLAIMS_TOTAL:
            break

    claims_str = "\n".join(claim_lines) if claim_lines else "(none yet)"

    return f"""LISTENER CONTEXT (from their podcast vault — what they've heard so far):

Topics they've explored most, with episode counts:
{topics_str}

Recent ideas/claims they've already been exposed to:
{claims_str}
"""


def suggest_related_slugs(notes: list[Note], article_topics: list[str], limit: int = 5) -> list[str]:
    """Find existing vault notes that share topics with a new article."""
    if not article_topics:
        return []
    article_set = {t.lower() for t in article_topics}
    scored: list[tuple[int, Note]] = []
    for n in notes:
        overlap = len(article_set & {t.lower() for t in n.topics})
        if overlap:
            scored.append((overlap, n))
    scored.sort(key=lambda pair: (-pair[0], pair[1].date or ""))
    return [n.slug for _, n in scored[:limit]]
