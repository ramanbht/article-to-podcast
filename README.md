# Podcast

Share an article URL from your iPhone; a few minutes later, a new episode shows
up in your personal podcast feed. Plays in Apple Podcasts like any normal show
— queue, AirPods, CarPlay, lock screen, 1.5×, sleep timer, the lot.

Uses **Claude** (via the standalone Claude Code CLI, which honors your existing
Claude Pro/Max subscription — no per-token API billing) to write a tight
script that keeps only the interesting bits, and **Kokoro** (local neural TTS)
to narrate locally on your Mac. Optionally publishes MP3s + RSS to a free
**Backblaze B2** bucket so Apple Podcasts can subscribe over the public
internet (with real HTTPS).

Also maintains an **Obsidian-/Logseq-compatible knowledge vault**: one
markdown note per article with summary, topic tags, and atomic claims. Future
scripts get told what you've already heard, so they soft-trim or briefly
reference rather than rehash. When multiple URLs arrive together, the daemon
checks whether to combine them into a single synthesized episode.

**Ongoing cost: $0** beyond your existing Claude subscription.

---

## Architecture

```
iPhone                                    Mac
─────────                                 ─────────────────────────────────
Apple Shortcut "Podcast this"
  ├─ writes request-*.json
  └─    to iCloud Drive  ◀──iCloud sync──▶  Python daemon
        Podcast/inbox/                       │  trafilatura  → article body
                                             │  claude (group) → batch related
                                             │  claude (script) → script + meta
                                             │  kokoro-onnx  → WAV
                                             │  ffmpeg       → MP3
                                             │
                                             ├─ writes vault/{date}-{slug}.md
                                             │     (Obsidian-compatible)
                                             │
                                             └─ uploads to Backblaze B2
                                                  episodes/{id}.mp3
                                                  feed.xml
                                                              │
Apple Podcasts subscribed to                                   │
https://f005.backblazeb2.com/file/                ◀────────────┘
   <bucket>/feed.xml
```

**Submission queue when Mac is asleep:** the Shortcut writes to iCloud Drive.
The file waits there until both devices come online and your Mac wakes; then
the daemon catches up. Nothing is lost.

**Episode delivery when Mac is asleep:** episodes live in the B2 bucket, so
Apple Podcasts can pull them anytime regardless of your Mac's state.

---

## Prerequisites

- macOS (tested on Apple Silicon; should work on Intel)
- A Claude Pro or Max subscription
- [Homebrew](https://brew.sh/) for `espeak-ng` and `ffmpeg`
- [uv](https://github.com/astral-sh/uv) for Python venv management
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `ffmpeg` (`brew install ffmpeg`)
- Node.js, for installing the Claude Code CLI
  (`npm install -g @anthropic-ai/claude-code`)
- A [Backblaze](https://www.backblaze.com/sign-up/cloud-storage) account (free,
  for B2 publishing — not strictly required if you only want local listening)

---

## Setup

### 1. Log in to Claude Code

```bash
claude /login
```

Sign in with your existing Anthropic account. The daemon shells out to `claude`
headlessly using your **Pro/Max subscription** — no API key, no API billing.

### 2. Install daemon dependencies

```bash
cd daemon
./setup.sh
```

Installs `espeak-ng` (Kokoro's phonemizer), creates a Python 3.11 venv,
installs Python deps (trafilatura, kokoro-onnx, soundfile, numpy — all
declared in [requirements.txt](daemon/requirements.txt)), and downloads the
Kokoro model files (~340 MB into `daemon/models/`).

### 3. Set up Backblaze B2 (free)

This is what makes Apple Podcasts work from anywhere. Skip if you only want
local-Wi-Fi access via the daemon's HTTP server.

1. Create a free account at https://www.backblaze.com/sign-up/cloud-storage.
   No credit card required for the 10 GB free tier.
2. In the B2 web UI: **Buckets → Create a Bucket**.
   - Name: pick anything unique, e.g. `raman-podcast-abc123`.
   - Files in Bucket are: **Public** (Apple Podcasts needs to fetch them).
   - Default Encryption: Disable (simpler) or SSE-B2 — both work.
3. Generate an Application Key: **Application Keys → Add a New Application Key**.
   - Name of Key: `podcast-daemon`
   - Allow access to: **just the bucket you created**
   - Type of Access: **Read and Write**
   - Click *Create New Key* and **copy the `keyID` and `applicationKey` shown
     once** — you cannot retrieve `applicationKey` later.
4. Find your bucket's public URL pattern by clicking the bucket → "Browse
   Files" → any file → "Friendly URL". It looks like:
   ```
   https://f005.backblazeb2.com/file/<bucket-name>/<file-path>
   ```
   Copy everything before `/<file-path>` — that's your `PODCAST_PUBLIC_URL`.

### 4. Install the daemon as a LaunchAgent

```bash
cd daemon
PODCAST_B2_KEY_ID="your-key-id" \
PODCAST_B2_APP_KEY="your-app-key" \
PODCAST_B2_BUCKET="your-bucket-name" \
PODCAST_PUBLIC_URL="https://f005.backblazeb2.com/file/your-bucket-name" \
  ./install-agent.sh
```

Bakes those env vars into a LaunchAgent plist at
`~/Library/LaunchAgents/local.podcast.daemon.plist`. The daemon now starts at
every login. Logs at `~/Library/Logs/PodcastDaemon/`.

Verify it's running:
```bash
curl http://localhost:8765/healthz   # → ok
launchctl list | grep podcast
tail -f ~/Library/Logs/PodcastDaemon/daemon.out.log
```

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/local.podcast.daemon.plist
```

### 5. Mac power settings

System Settings → Battery → **Options**:
- Turn on **"Prevent automatic sleeping on power adapter when the display is off"**
- Turn on **"Wake for network access"**

With B2 publishing, the Mac only needs to be awake to *generate* new episodes
— once uploaded, your phone fetches from B2 directly. Sleep doesn't break
playback or new-episode delivery.

### 6. Subscribe in Apple Podcasts

On your iPhone, in Safari, visit:
```
podcast://f005.backblazeb2.com/file/your-bucket-name/feed.xml
```
The `podcast://` scheme makes iOS prompt to open in Podcasts. Tap **Open** →
the app opens with your feed loaded → tap **Follow**.

(The feed is empty until the daemon produces and uploads its first episode.)

### 7. Build the "Podcast this" iPhone Shortcut

The share-sheet entry point: see an article anywhere → share → **Podcast this**.
The Shortcut writes a request file into iCloud Drive, which syncs to your Mac.
Works offline — the file queues until both devices are online.

Before building: in **Files** on iPhone, navigate to **iCloud Drive** and
create a folder **Podcast** with a subfolder **inbox** inside.

In the **Shortcuts** app:

1. **+** → new shortcut → name it **Podcast this**.
2. Tap the ⓘ at the bottom → toggle **Show in Share Sheet** ON. Under
   *Accepted Types*, leave only **URLs**.
3. Add these actions:

   | # | Action | Configure |
   |---|---|---|
   | 1 | **Get URLs from Input** | (Web category). No config. |
   | 2 | **Format Date** | (Date category). Date = **Current Date**. Format = **Custom**, format string `yyyyMMddHHmmss`. |
   | 3 | **Text** | (Documents category). Type: `{"url":"` then insert variable **URLs from Input**, then `","id":"`, insert variable **Formatted Date**, then `"}`. |
   | 4 | **Save File** | (Documents category). File: **Text** from step 3. Tap **Show More** → Service: iCloud Drive. Ask Where to Save: **off**. Destination Path: `/Podcast/inbox/`. File Name: type `request-`, insert **Formatted Date**, type `.json`. Overwrite: off. |

4. (Optional) Add a **Show Notification** action at the end.

Test it: in Safari, share any article → **Podcast this**. Within ~90s, the
daemon processes it, uploads MP3 + updated feed.xml to B2. Pull-to-refresh
the show in Apple Podcasts → Library — the new episode appears.

---

## How the smart features work

### Knowledge vault (`vault/`)

One markdown note per processed article (or per combined episode). Format is
Obsidian-/Logseq-compatible — frontmatter + body — so you can open the vault
folder as an Obsidian vault and get the graph view, backlinks, tag pane, and
full-text search for free.

```markdown
---
url: "https://paulgraham.com/greatwork.html"
title: How to Do Great Work
date: 2026-05-20
duration_sec: 429.3
episode_id: aaa
topics:
  - doing-great-work
  - ambition
  - curiosity
---

# Summary
2-3 sentences in plain prose.

# Key claims
- atomic claim 1
- atomic claim 2
- ...

# Related
- [[2026-05-04-some-related-article]]
```

The `[[wikilinks]]` in the *Related* section become edges in Obsidian's graph
view. Topic tags in YAML appear in Obsidian's tag pane.

**Using the vault inside your existing Obsidian setup:** point the daemon
at a subfolder of your main vault by setting `PODCAST_VAULT_DIR`:

```bash
PODCAST_VAULT_DIR="$HOME/Obsidian/MyVault/Podcast" \
  ./install-agent.sh
```

Your personal notes and the daemon's notes now share one graph. The daemon
only ever *creates* new notes — never modifies existing ones — so the risk
to your personal notes is minimal.

### Memory-aware scripts

Before generating each new script, the daemon loads the vault and builds a
listener-context summary: which topics you've explored most (with episode
counts), and the recent atomic claims you've already heard. That context is
passed to Claude in the script-generation prompt.

The default behavior is **soft trimming**: Claude briefly references already-
covered ideas (something like "you've already heard PG's framing of X — moving
on") rather than fully omitting them. When the article's topic clearly matches
something you've shown sustained interest in, Claude leans into the specifics.

Tune by editing `_MEMORY_HINT` in
[generate_script.py](daemon/generate_script.py).

### Combining related pending articles

When the daemon picks up multiple stable requests in one iteration (e.g.,
after waking from sleep with a backlog, or when you submit several articles
in quick succession), it asks Claude Haiku to judge whether any should be
combined into a single synthesized episode.

The grouping prompt is intentionally conservative — it combines only when
articles have strong topic/argument overlap. Default outcome is one episode
per article.

Tune the grouping prompt in [combine.py](daemon/combine.py).

---

## Optional: local listening on home Wi-Fi (no B2)

If you skip the B2 setup, the daemon still works in local-LAN mode:

```bash
PODCAST_PUBLIC_URL="http://Ramans-MacBook-Pro.local:8765" \
  ./install-agent.sh
```

The daemon serves `feed.xml` and `episodes/*.mp3` from your Mac directly.

**Caveat:** Apple Podcasts requires HTTPS with a trusted certificate, so
subscribing to a plain-HTTP local URL doesn't work in Apple Podcasts. Your
options for local-only are limited:
- **VLC for iOS** can play files directly from iCloud Drive
- **A web player** at `http://Ramans-MacBook-Pro.local:8765/` (add to home
  screen for an app-like icon — Safari plays HTTP fine)
- **mkcert + manual TLS cert installation on iPhone** for an Apple Podcasts
  experience without a third-party tunnel

The B2 path sidesteps all of this.

---

## Tweaks

- **Voice**: set `PODCAST_VOICE=am_michael` (or `bf_emma`, `af_bella`).
  Default `af_heart`. See [synthesize.py](daemon/synthesize.py).
- **Script model**: `PODCAST_SCRIPT_MODEL` (default `claude-opus-4-7`).
  Sonnet is faster and uses less quota; Opus produces noticeably better
  scripts on long-form articles.
- **Grouping model**: `PODCAST_GROUPING_MODEL` (default
  `claude-haiku-4-5-20251001`). Haiku is fine for the binary grouping
  decision and costs almost nothing.
- **Script tone / length**: edit `_BASE_RULES` in
  [generate_script.py](daemon/generate_script.py).
- **Memory aggressiveness**: edit `_MEMORY_HINT` to switch between soft trim
  (default) and aggressive omission.
- **Feed name/author**: `PODCAST_FEED_TITLE`, `PODCAST_FEED_AUTHOR`,
  `PODCAST_FEED_DESCRIPTION` — re-run `install-agent.sh`.
- **Vault location**: `PODCAST_VAULT_DIR`. Point at a folder inside an
  existing Obsidian vault to unify graphs.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Shortcut succeeds but no episode appears | `tail -f ~/Library/Logs/PodcastDaemon/daemon.out.log`. Most common: `claude` not logged in. Run `claude /login`. |
| Apple Podcasts won't load the feed | Verify the feed URL works in a browser. Make sure the B2 bucket is set to Public. |
| B2 uploads fail | Confirm the application key has Read+Write access to the bucket. Look for HTTP 401/403 in the daemon log. |
| Daemon error "Kokoro model files missing" | Re-run `daemon/setup.sh`. |
| Combined episodes happening when you didn't want them | The grouping prompt is conservative but not perfect. Edit `GROUPING_PROMPT` in [combine.py](daemon/combine.py) to be stricter. |
| Vault notes have weird YAML escaping | The YAML writer is hand-rolled and minimal. File an issue with the problematic title. |

---

## File layout

```
daemon/
  config.py            shared paths + env config (incl. B2 creds)
  extract.py           URL → article text  (trafilatura)
  generate_script.py   article(s) → script + metadata (claude CLI)
  combine.py           grouping decision via claude (haiku)
  synthesize.py        script → WAV        (kokoro-onnx)
  vault.py             markdown notes (Obsidian/Logseq format)
  memory.py            context summary from vault
  upload.py            Backblaze B2 native API via urllib
  daemon.py            watcher + batch orchestration
  server.py            HTTP + RSS feed (also generates feed.xml for B2)
  setup.sh             one-time deps + model download
  install-agent.sh     LaunchAgent install (bakes env vars in)
  requirements.txt
  models/              Kokoro ONNX + voices (gitignored)
```

Runtime data:
- **Inbox** (iCloud Drive): `~/Library/Mobile Documents/com~apple~CloudDocs/Podcast/inbox/`
- **Episodes** (local cache): `~/Library/Application Support/PodcastDaemon/episodes/`
- **Vault** (markdown notes): `~/Library/Application Support/PodcastDaemon/vault/` (override with `PODCAST_VAULT_DIR`)
- **Logs**: `~/Library/Logs/PodcastDaemon/`

---

## License

[MIT](LICENSE).
