"""Turn an article into a tight podcast script by calling the `claude` CLI.

The CLI uses the user's logged-in Pro/Max session — no API key, no API billing.
"""
from __future__ import annotations

import subprocess

CLAUDE_BIN = "/usr/local/bin/claude"
MODEL = "claude-opus-4-7"
TIMEOUT_SEC = 600

PROMPT_TEMPLATE = """You are an audio editor producing a personal podcast for a curious, time-pressed reader. The user saved this article to listen to instead of reading.

Your job: produce a podcast script that captures ONLY the interesting, surprising, or non-obvious content. Aggressively skip:
- filler, ads, navigation, author bios, subscription pitches
- generic background a well-read reader would already know
- standard caveats, hedging, polite throat-clearing
- repetition (articles often restate the same point 3 times — keep it once)

The script should sound like a smart friend explaining the meat of the piece in their own words. Conversational, dense, no fluff. Dive straight in — no "in this article" or "the author argues", just present the ideas directly. End when the ideas end; no outro.

Length: aim for ~3-6 minutes of speech (roughly 450-900 words). If the article genuinely has less interesting content, go shorter. Never pad.

Output: ONLY the script text. No preamble. No markdown. No stage directions. No headings. Plain prose that will be read aloud by a TTS engine, so avoid characters that don't read well (—, …, *, #, etc. — use regular punctuation).

Article title: {title}
Article URL: {url}

---ARTICLE TEXT---
{body}
---END ARTICLE---

Now produce the script."""


def generate_script(url: str, title: str, body: str) -> str:
    """Invoke `claude -p` and return the script text. Raises on failure."""
    # Trim very long bodies to keep the prompt sane.
    if len(body) > 80_000:
        body = body[:80_000] + "\n[... article truncated ...]"

    prompt = PROMPT_TEMPLATE.format(title=title, url=url, body=body)

    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--model", MODEL],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    script = result.stdout.strip()
    if len(script) < 100:
        raise RuntimeError(f"claude returned suspiciously short script: {script!r}")
    return script
