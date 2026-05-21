"""Group related pending articles so we produce one synthesized episode
rather than several separate scripts on the same topic.

Conservative by default: groups only when topic overlap is strong. When
unsure, leaves articles separate.
"""
from __future__ import annotations

import json
import re
import subprocess

import config


GROUPING_PROMPT = """You are helping decide whether several pending podcast articles should be combined into a single synthesized episode, or kept as separate episodes.

Combine ONLY when articles meaningfully overlap — same topic, same debate, complementary angles on one question. Don't combine articles that are merely "vaguely related" or in the same broad domain. When in doubt, keep them separate.

Articles to group:
{article_list}

Respond with ONLY a JSON array of arrays, where each inner array is the IDs of articles to bundle. Every input ID must appear in exactly one inner array. Example for 3 inputs where the first two combine:
[["a"], ["b", "c"]]
or, if all three combine:
[["a", "b", "c"]]
or, if none combine:
[["a"], ["b"], ["c"]]

Reply with just the JSON, no prose."""


def _format_articles(articles: list[dict]) -> str:
    lines = []
    for a in articles:
        # Trim body to a snippet for cheap grouping decision.
        snippet = (a.get("body") or "").strip().replace("\n", " ")[:600]
        lines.append(f'- id: "{a["id"]}"\n  title: {a.get("title") or "(no title)"}\n  url: {a.get("url")}\n  snippet: {snippet!r}')
    return "\n".join(lines)


def _call_claude(prompt: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["/usr/local/bin/claude", "-p", prompt, "--model", config.GROUPING_MODEL],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude grouping failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _parse_groups(raw: str, all_ids: list[str]) -> list[list[str]]:
    """Best-effort JSON parsing with sanity checks. Fall back to all-separate
    on any malformed response — we'd rather not combine than combine wrong."""
    # Strip any markdown fences if the model added them.
    m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
    if not m:
        return [[i] for i in all_ids]
    try:
        groups = json.loads(m.group(0))
    except Exception:
        return [[i] for i in all_ids]
    if not isinstance(groups, list) or not all(isinstance(g, list) for g in groups):
        return [[i] for i in all_ids]

    # Validate: every input id appears exactly once.
    seen: set[str] = set()
    flat: list[str] = []
    for g in groups:
        for x in g:
            if not isinstance(x, str) or x in seen or x not in set(all_ids):
                return [[i] for i in all_ids]
            seen.add(x)
            flat.append(x)
    if seen != set(all_ids):
        return [[i] for i in all_ids]
    return [list(g) for g in groups if g]


def group_articles(articles: list[dict]) -> list[list[dict]]:
    """Return articles partitioned into groups. Each group becomes one episode."""
    if len(articles) <= 1:
        return [list(articles)] if articles else []

    all_ids = [a["id"] for a in articles]
    prompt = GROUPING_PROMPT.format(article_list=_format_articles(articles))

    try:
        raw = _call_claude(prompt)
    except Exception:
        # If the grouping call fails for any reason, default to keeping separate.
        return [[a] for a in articles]

    id_groups = _parse_groups(raw, all_ids)
    by_id = {a["id"]: a for a in articles}
    return [[by_id[i] for i in g] for g in id_groups]
