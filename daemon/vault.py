"""Knowledge vault: one markdown note per processed article.

Format is Obsidian-/Logseq-compatible:

  ---
  url: https://example.com
  title: How to Do Great Work
  date: 2026-05-20
  duration_sec: 320
  episode_id: 1b35c3402fa0
  topics:
    - great-work
    - ambition
    - choosing-a-field
  ---

  # Summary
  Two-or-three-sentence summary.

  # Key claims
  - claim 1
  - claim 2

  # Related
  - [[2026-05-04-some-other-article]]

The YAML subset we read/write is intentionally minimal — string scalars and
flat lists of strings. We hand-roll it to avoid the pyyaml dependency.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

import config


@dataclass
class Note:
    path: Path
    slug: str
    frontmatter: dict
    body: str

    @property
    def title(self) -> str:
        return self.frontmatter.get("title") or self.slug

    @property
    def url(self) -> str:
        return self.frontmatter.get("url") or ""

    @property
    def date(self) -> str:
        return str(self.frontmatter.get("date") or "")

    @property
    def topics(self) -> list[str]:
        t = self.frontmatter.get("topics")
        return list(t) if isinstance(t, list) else []

    @property
    def key_claims(self) -> list[str]:
        return _extract_bulleted_section(self.body, "Key claims")

    @property
    def summary(self) -> str:
        section = _extract_section(self.body, "Summary")
        return section.strip()


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_SLUG_NONWORD = re.compile(r"[^\w\s-]+", flags=re.UNICODE)
_SLUG_WS = re.compile(r"[\s_-]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Lower-cased, hyphen-separated ASCII slug. Empty input → 'untitled'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = _SLUG_NONWORD.sub("", text).strip().lower()
    text = _SLUG_WS.sub("-", text).strip("-")
    text = text[:max_len].rstrip("-")
    return text or "untitled"


def note_slug(date_str: str, title: str) -> str:
    return f"{date_str}-{slugify(title)}"


# ---------------------------------------------------------------------------
# Minimal YAML serialization for our flat schema
# ---------------------------------------------------------------------------

def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote when value contains characters that would confuse a YAML parser.
    if s == "" or any(c in s for c in ":#\n\"'[]{}&*!|>%@`") or s[0] in "-?,":
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _yaml_dump(d: dict) -> str:
    lines: list[str] = []
    for k, v in d.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {_yaml_scalar(item)}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    return "\n".join(lines) + "\n"


def _yaml_parse(text: str) -> dict:
    """Parse the limited YAML subset we write. Tolerates simple lists."""
    out: dict = {}
    current_list_key: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("  - ") or raw.startswith("- "):
            if current_list_key is None:
                continue
            val = raw.split("-", 1)[1].strip()
            val = _yaml_unquote(val)
            out.setdefault(current_list_key, []).append(val)
            continue
        if ":" in raw:
            key, _, val = raw.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "" or val is None:
                current_list_key = key
                out[key] = []
            else:
                current_list_key = None
                out[key] = _yaml_unquote(val)
    return out


def _yaml_unquote(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s.lower() == "null":
        return None  # type: ignore[return-value]
    if s.isdigit():
        return int(s)  # type: ignore[return-value]
    return s


# ---------------------------------------------------------------------------
# Body section extraction (for memory queries)
# ---------------------------------------------------------------------------

def _extract_section(body: str, heading: str) -> str:
    pat = re.compile(rf"^#\s+{re.escape(heading)}\s*$", flags=re.MULTILINE)
    m = pat.search(body)
    if not m:
        return ""
    rest = body[m.end():]
    # End at the next "# Heading" line.
    next_h = re.search(r"^#\s+", rest, flags=re.MULTILINE)
    return rest[: next_h.start()] if next_h else rest


def _extract_bulleted_section(body: str, heading: str) -> list[str]:
    section = _extract_section(body, heading)
    items: list[str] = []
    for line in section.splitlines():
        s = line.strip()
        if s.startswith("- "):
            items.append(s[2:].strip())
    return items


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", flags=re.DOTALL)


def parse_note(path: Path) -> Note | None:
    try:
        text = path.read_text()
    except Exception:
        return None
    m = _FRONT_RE.match(text)
    if not m:
        return Note(path=path, slug=path.stem, frontmatter={}, body=text)
    fm = _yaml_parse(m.group(1))
    body = m.group(2)
    return Note(path=path, slug=path.stem, frontmatter=fm, body=body)


def load_all_notes(vault_dir: Path | None = None) -> list[Note]:
    vd = vault_dir or config.VAULT_DIR
    if not vd.exists():
        return []
    notes: list[Note] = []
    for p in sorted(vd.glob("*.md")):
        n = parse_note(p)
        if n is not None:
            notes.append(n)
    return notes


def save_note(
    slug: str,
    url: str,
    title: str,
    date: str,
    duration_sec: float,
    episode_id: str,
    topics: list[str],
    summary: str,
    key_claims: list[str],
    related_slugs: list[str] | None = None,
    extra: dict | None = None,
    vault_dir: Path | None = None,
) -> Path:
    vd = vault_dir or config.VAULT_DIR
    vd.mkdir(parents=True, exist_ok=True)
    fm: dict = {
        "url": url,
        "title": title,
        "date": date,
        "duration_sec": round(float(duration_sec), 1),
        "episode_id": episode_id,
        "topics": list(topics or []),
    }
    if extra:
        fm.update(extra)
    body_parts = [
        "# Summary\n",
        (summary.strip() + "\n") if summary else "(no summary)\n",
        "\n# Key claims\n",
    ]
    if key_claims:
        for c in key_claims:
            body_parts.append(f"- {c.strip()}\n")
    else:
        body_parts.append("(no key claims)\n")
    if related_slugs:
        body_parts.append("\n# Related\n")
        for s in related_slugs:
            body_parts.append(f"- [[{s}]]\n")
    body = "".join(body_parts)
    text = f"---\n{_yaml_dump(fm)}---\n\n{body}"
    path = vd / f"{slug}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return path


def today_str() -> str:
    return _date.today().isoformat()
