"""Turn one or more articles into a podcast script + structured metadata.

The script is plain prose for TTS. The metadata feeds the knowledge vault
(topics, summary, key_claims). Both are returned by a single Claude call to
save quota and keep them mutually consistent.

The `memory_context` argument is the listener-history block from memory.py;
pass an empty string to disable memory-aware behavior for a particular call.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

import config

CLAUDE_BIN = "/usr/local/bin/claude"
TIMEOUT_SEC = 900
MAX_BODY_CHARS = 80_000  # per article


@dataclass
class ScriptResult:
    script: str
    topics: list[str]
    summary: str
    key_claims: list[str]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_BASE_RULES = """You are an audio editor producing a personal podcast for a curious, time-pressed reader. The user saved the article(s) below to listen to instead of reading.

Your job: produce a podcast script that captures ONLY the interesting, surprising, or non-obvious content. Aggressively skip:
- filler, ads, navigation, author bios, subscription pitches
- generic background a well-read reader would already know
- standard caveats, hedging, polite throat-clearing
- repetition (articles often restate the same point 3 times — keep it once)

Script style: sound like a smart friend explaining the meat in their own words. Conversational, dense, no fluff. Dive straight in — no "in this article" or "the author argues", just present the ideas directly. End when the ideas end; no outro.

Length: aim for ~3-6 minutes of speech (roughly 450-900 words) per article. For combined episodes, scale up modestly — don't just add lengths together; synthesize.

TTS hygiene: no markdown, no headings, no stage directions, no emoji. Avoid characters that don't read well aloud (—, …, *, #). Use regular punctuation. Spell out numbers and acronyms when ambiguous."""

_MEMORY_HINT = """The listener has heard the things below in past episodes. When you encounter the SAME ideas in this article:
- skip them, or
- briefly reference them ("you've already heard this framing") and move on to the new material
DO NOT fully omit context that's load-bearing for what comes next; soft trim only. When the article's topic clearly matches their established interests, lean into specifics they'd care about. When something is tangential to their interests, trim it shorter."""

_COMBINED_RULES = """COMBINED EPISODE MODE: these articles are related. Produce ONE synthesized script — not separate sections. Find the throughline. Note where the articles agree, disagree, or complement each other. Don't just stitch summaries together; weave them."""

_OUTPUT_FORMAT = """Output exactly two blocks, in this order, with no other text before or after:

<script>
[the narration script, plain prose, ready for TTS]
</script>

<metadata>
{
  "topics": ["lowercase-hyphenated-tags", "..."],
  "summary": "2-3 sentence summary in plain prose",
  "key_claims": [
    "one concise statement of an idea/claim from the article",
    "another claim",
    "..."
  ]
}
</metadata>

Topic tag guidance:
- 3-7 tags. Lowercase, hyphenated. Specific over generic ("agi-alignment" not "ai").
- Reuse common tags across articles when applicable; only invent new tags for genuinely new themes.

Key claims guidance: 5-15 short, atomic, declarative statements of what the article asserts. Each should make sense on its own."""


def _build_prompt(articles: list[dict], memory_context: str) -> str:
    combined = len(articles) > 1

    article_blocks: list[str] = []
    for i, a in enumerate(articles, 1):
        body = (a.get("body") or "").strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n[... truncated ...]"
        label = f"ARTICLE {i}" if combined else "ARTICLE"
        article_blocks.append(
            f"---{label}---\nTitle: {a.get('title') or '(no title)'}\nURL: {a.get('url')}\n\n{body}\n---END {label}---"
        )

    parts = [_BASE_RULES]
    if memory_context:
        parts.append(_MEMORY_HINT)
        parts.append(memory_context)
    if combined:
        parts.append(_COMBINED_RULES)
    parts.append(_OUTPUT_FORMAT)
    parts.append("\n\n".join(article_blocks))
    parts.append("Now produce the two blocks.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Claude call + response parsing
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script>\s*(.*?)\s*</script>", flags=re.DOTALL | re.IGNORECASE)
_METADATA_RE = re.compile(r"<metadata>\s*(.*?)\s*</metadata>", flags=re.DOTALL | re.IGNORECASE)


class ClaudeDeclinedError(RuntimeError):
    """Claude returned a response with no <script> block. Usually means the
    article body was junk (error page, paywall stub, etc.) and Claude refused
    to fabricate content. Daemon handles cleanly."""


class RetriableError(RuntimeError):
    """A transient, fixable failure (expired `claude` login, network blip).
    The daemon should NOT consume the request — keep it and retry later."""


# Substrings in the claude CLI's stderr/stdout that mean "not your article's
# fault, try again after fixing auth / connectivity".
_RETRIABLE_MARKERS = (
    "invalid authentication credentials",
    "please run /login",
    "not logged in",
    "401",
    "rate limit",
    "429",
    "overloaded",
    "529",
    "connection error",
    "timeout",
)


def _is_retriable(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _RETRIABLE_MARKERS)


def _parse_response(raw: str) -> ScriptResult:
    s_match = _SCRIPT_RE.search(raw)
    if not s_match:
        # Surface the first line of Claude's explanation as the error message.
        refusal = " ".join(raw.strip().split())[:300]
        raise ClaudeDeclinedError(
            f"Claude declined to script (likely the fetched body wasn't real article content): {refusal}"
        )
    script = s_match.group(1).strip()
    if len(script) < 100:
        raise RuntimeError(f"script suspiciously short: {script!r}")

    topics: list[str] = []
    summary = ""
    claims: list[str] = []
    m_match = _METADATA_RE.search(raw)
    if m_match:
        try:
            meta = json.loads(m_match.group(1).strip())
            topics = [str(t).strip().lower() for t in (meta.get("topics") or []) if str(t).strip()]
            summary = str(meta.get("summary") or "").strip()
            claims = [str(c).strip() for c in (meta.get("key_claims") or []) if str(c).strip()]
        except Exception:
            # Don't fail the whole script over a bad metadata block.
            pass

    return ScriptResult(script=script, topics=topics, summary=summary, key_claims=claims)


def generate_script(articles: list[dict], memory_context: str = "") -> ScriptResult:
    """Generate one script (+ metadata) from one or more related articles.

    `articles` is a list of dicts: {"url", "title", "body"}.
    Pass a single-element list for normal mode, multi-element for combined.
    """
    if not articles:
        raise ValueError("generate_script needs at least one article")

    prompt = _build_prompt(articles, memory_context)
    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--model", config.SCRIPT_MODEL],
        capture_output=True, text=True, timeout=TIMEOUT_SEC,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if _is_retriable(detail):
            raise RetriableError(f"claude CLI transient failure: {detail}")
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {detail}")
    return _parse_response(result.stdout.strip())
