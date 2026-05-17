# Podcast

Share an article URL from your iPhone; a few minutes later, a new episode shows
up in your personal podcast feed. Plays in Apple Podcasts like any normal show
— queue, AirPods, CarPlay, lock screen, 1.5×, sleep timer, the lot.

Uses **Claude** (via the standalone Claude Code CLI, which honors your existing
Claude Pro/Max subscription — no per-token API billing) to write a tight
script that keeps only the interesting bits, and **Kokoro** (local neural TTS)
to narrate. Everything runs on your Mac. No cloud, no app store, no $99/year.

**Ongoing cost: $0** beyond your existing Claude subscription.

---

## Architecture

```
iPhone                                    Mac (when awake, on home Wi-Fi)
─────────                                 ───────────────────────────────
Apple Shortcut "Podcast this"
  ├─ writes request-*.json
  └─    to iCloud Drive  ◀──iCloud sync──▶  Python daemon
        Podcast/inbox/                       │  watches inbox
                                             │  trafilatura → article body
                                             │  claude CLI   → script (Pro plan)
                                             │  kokoro-onnx  → WAV
                                             │  ffmpeg       → MP3
                                             │
                                             └─ writes ~/Library/Application Support/
                                                  PodcastDaemon/episodes/{id}.mp3
                                                              │
Apple Podcasts subscribed to                HTTP server (same daemon)
http://<your-mac>.local:8765/                  GET /feed.xml      → RSS 2.0
   feed.xml ◀───home Wi-Fi────                GET /episodes/*.mp3 → audio
                                               POST /submit       → enqueue URL
                                                                    (browser/curl)
```

**How submissions work when the Mac is asleep:** the Shortcut writes a file
into iCloud Drive. The file queues there until your Mac wakes; the daemon
catches up on the backlog automatically. Nothing is lost.

**How listening works away from home:** episodes are auto-downloaded by Apple
Podcasts whenever your phone is on home Wi-Fi (overnight, evenings, etc.).
Once downloaded they live on your phone — playback works offline, anywhere.
If you want new episodes to appear from outside your home network too, see
the [Tailscale](#optional-tailscale) section.

---

## Prerequisites

- macOS (tested on Apple Silicon; should work on Intel)
- A Claude Pro or Max subscription
- [Homebrew](https://brew.sh/) for `espeak-ng`
- [uv](https://github.com/astral-sh/uv) for Python venv management
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `ffmpeg` (`brew install ffmpeg`)
- Node.js, for installing the Claude Code CLI
  (`npm install -g @anthropic-ai/claude-code`)

---

## Setup

### 1. Log in to Claude Code

```bash
claude /login
```

Sign in with your existing Anthropic account. The daemon shells out to `claude`
headlessly using your **Pro/Max subscription** — no API key, no API billing.

### 2. Install dependencies

```bash
cd daemon
./setup.sh
```

This installs `espeak-ng` (Kokoro's phonemizer), creates a Python 3.11 venv,
installs Python deps, and downloads the Kokoro model files (~340 MB into
`daemon/models/`).

### 3. Find your Mac's local hostname

```bash
scutil --get LocalHostName
```

Prints something like `my-mac` — your phone will reach the Mac at
`http://my-mac.local:8765` on the home network.

### 4. Run the daemon at login

```bash
cd daemon
PODCAST_PUBLIC_URL="http://my-mac.local:8765" ./install-agent.sh
```

(Replace `my-mac` with your hostname from step 3.) Loads a LaunchAgent that
starts the daemon at every login. Logs go to `~/Library/Logs/PodcastDaemon/`.

Verify it's running:
```bash
curl http://localhost:8765/healthz   # → ok
```

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/local.podcast.daemon.plist
```

### 5. Mac power settings

System Settings → Battery → **Options**:
- Turn on **"Prevent automatic sleeping on power adapter when the display is off"**
- Turn on **"Wake for network access"**

These help the Mac stay reachable when plugged in with the lid open.

### 6. Subscribe in Apple Podcasts

On your iPhone, **while on home Wi-Fi**:

1. Open Safari, visit `http://my-mac.local:8765/feed.xml` (your hostname).
   Safari shows raw XML — that's fine.
2. Tap the share icon → scroll down → **Open in Podcasts**.
   (Or long-press the address bar for the same option.)
3. The Podcasts app opens with the feed loaded. Tap **Follow**.

(On a Mac, in Apple Podcasts: **File → Follow a Show by URL** is the same.)

### 7. Build the "Podcast this" iPhone Shortcut

The share-sheet entry point: see an article in Safari/Reader/Twitter → share →
**Podcast this**. The Shortcut writes a request file into iCloud Drive, which
syncs to your Mac. Works offline (the file queues until both devices are
online).

Before building: in the **Files** app on iPhone, navigate to **iCloud Drive**
and create a folder **Podcast** with a subfolder **inbox** inside it.

In the **Shortcuts** app:

1. **+** (top right) → new shortcut → name it **Podcast this**.
2. Tap the ⓘ at the bottom → toggle **Show in Share Sheet** ON. Under
   *Accepted Types*, leave only **URLs**.
3. Add these actions:

   | # | Action | Configure |
   |---|---|---|
   | 1 | **Get URLs from Input** | (Web category). No config. |
   | 2 | **Format Date** | (Date category). Date = **Current Date**. Format = **Custom**, format string `yyyyMMddHHmmss`. |
   | 3 | **Text** | (Documents category). Type: `{"url":"` then insert variable **URLs from Input**, then `","id":"`, insert variable **Formatted Date**, then `"}`. |
   | 4 | **Save File** | (Documents category). File: **Text** from step 3. Tap **Show More** → Service: iCloud Drive. Ask Where to Save: **off**. Destination Path: `/Podcast/inbox/`. File Name: type `request-`, insert **Formatted Date**, type `.json`. Overwrite: off. |

4. (Optional) Add a **Show Notification** action at the end: "Queued for podcast".

Done. Test it: in Safari, share any article → **Podcast this**. A
`request-YYYYMMDDHHMMSS.json` should appear in Files → iCloud Drive →
Podcast → inbox.

Within ~60–90s (assuming Mac is awake and on Wi-Fi), the daemon log shows
it processing, and the next time Apple Podcasts polls, the new episode
appears in your Library.

---

## Optional: Tailscale

The default setup uses `.local` mDNS hostnames, which only resolve on your
home Wi-Fi. That means **new episodes only flow to your phone when it's on
home Wi-Fi.** Episodes you've already downloaded play anywhere, so most
people are fine with this — submit during the day, listen on the commute.

If you want new episodes to be pulled even from a coffee shop or on cellular,
install [Tailscale](https://tailscale.com/) (free for personal use) on both
your Mac and iPhone, then re-run the install script with your tailnet
hostname:

```bash
tailscale status | head -1   # find your tailnet hostname
cd daemon
PODCAST_PUBLIC_URL="http://my-mac.tail-xxxx.ts.net:8765" ./install-agent.sh
```

Re-subscribe in Apple Podcasts to the new feed URL. Nothing else changes.

---

## Tweaks

- **Voice**: set `PODCAST_VOICE=am_michael` (or `bf_emma`, `af_bella`, etc.)
  and re-run `install-agent.sh`. Defaults to `af_heart`.
- **Script tone/length**: edit `PROMPT_TEMPLATE` in
  [generate_script.py](daemon/generate_script.py). The default targets
  3–6 minutes and tells Claude to skip filler.
- **Model**: edit `MODEL` in [generate_script.py](daemon/generate_script.py).
  Opus produces noticeably better scripts than Sonnet for long-form articles
  but uses more of your subscription quota.
- **Feed name/author**: set `PODCAST_FEED_TITLE`, `PODCAST_FEED_AUTHOR`,
  `PODCAST_FEED_DESCRIPTION` and re-run `install-agent.sh`.
- **Port**: set `PODCAST_HTTP_PORT`. Default 8765.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Shortcut succeeds but no episode appears | `tail -f ~/Library/Logs/PodcastDaemon/daemon.out.log`. Most common: `claude` not logged in. Run `claude /login`. |
| Apple Podcasts won't load the feed | Make sure both devices are on the same Wi-Fi (without Tailscale). Test by opening the feed URL in Safari on your phone. |
| Mac unreachable when phone is on cellular | Expected behavior with the LAN-only setup. Install [Tailscale](#optional-tailscale) if it bothers you. |
| Daemon error "Kokoro model files missing" | Re-run `daemon/setup.sh`. |
| Audio sounds robotic | Try a different voice via `PODCAST_VOICE`. |
| Article extraction returns mostly junk | Heavy-JS sites and paywalls don't work — `trafilatura` needs HTML body. |

---

## File layout

```
daemon/
  config.py            shared paths + env config
  extract.py           URL → article text  (trafilatura)
  generate_script.py   article → script    (claude CLI)
  synthesize.py        script → WAV        (kokoro-onnx)
  daemon.py            watcher loop + WAV→MP3 (ffmpeg)
  server.py            HTTP + RSS feed
  setup.sh             one-time deps + model download
  install-agent.sh     LaunchAgent install
  requirements.txt
  models/              Kokoro ONNX + voices (gitignored)
```

Runtime data lives outside the repo:
- **Inbox** (iCloud Drive, shared with phone): `~/Library/Mobile Documents/com~apple~CloudDocs/Podcast/inbox/`
- **Episodes** (local-only, HTTP-served): `~/Library/Application Support/PodcastDaemon/episodes/`
- **Logs**: `~/Library/Logs/PodcastDaemon/`

---

## License

[MIT](LICENSE).
