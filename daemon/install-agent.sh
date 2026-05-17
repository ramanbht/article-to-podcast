#!/usr/bin/env bash
# Install and load the LaunchAgent so the daemon runs at login.
#
# Required env (or use --public-url ...):
#   PODCAST_PUBLIC_URL  full URL the iPhone hits (e.g. http://my-mac.tailnet-xxx.ts.net:8765)
#
# Optional env:
#   PODCAST_HTTP_PORT     default 8765
#   PODCAST_INBOX         default: iCloud Drive/Podcast/inbox
#   PODCAST_DATA_DIR      default: ~/Library/Application Support/PodcastDaemon
#   PODCAST_FEED_TITLE, PODCAST_FEED_DESCRIPTION, PODCAST_FEED_AUTHOR, PODCAST_VOICE
set -euo pipefail

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$DAEMON_DIR/.venv"
LABEL="${PODCAST_AGENT_LABEL:-local.podcast.daemon}"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/PodcastDaemon"

if [ ! -x "$VENV_DIR/bin/python3" ]; then
  echo "ERROR: venv not found at $VENV_DIR. Run setup.sh first." >&2
  exit 1
fi

# Allow --public-url=... as a friendlier arg form.
for arg in "$@"; do
  case "$arg" in
    --public-url=*) PODCAST_PUBLIC_URL="${arg#*=}";;
    --port=*) PODCAST_HTTP_PORT="${arg#*=}";;
  esac
done

if [ -z "${PODCAST_PUBLIC_URL:-}" ]; then
  echo "WARNING: PODCAST_PUBLIC_URL not set — will fall back to <hostname>.local:8765."
  echo "         Apple Podcasts on cellular won't be able to reach that."
  echo "         For Tailscale, find your hostname with: tailscale status"
  echo "         Then re-run:  PODCAST_PUBLIC_URL=http://<host>:8765 ./install-agent.sh"
  echo
fi

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# Build the EnvironmentVariables block from any PODCAST_* env we have.
ENV_KV=""
for v in PODCAST_PUBLIC_URL PODCAST_HTTP_PORT PODCAST_INBOX PODCAST_DATA_DIR \
         PODCAST_FEED_TITLE PODCAST_FEED_DESCRIPTION PODCAST_FEED_AUTHOR PODCAST_VOICE; do
  val="${!v:-}"
  if [ -n "$val" ]; then
    # XML-escape & < > " for the plist
    esc=$(printf '%s' "$val" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g')
    ENV_KV+="    <key>$v</key><string>$esc</string>
"
  fi
done

cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_DIR/bin/python3</string>
    <string>$DAEMON_DIR/daemon.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DAEMON_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
$ENV_KV  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/daemon.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/daemon.err.log</string>
</dict>
</plist>
EOF

echo "==> wrote $PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "==> launchctl loaded $LABEL"
echo
echo "Tail logs:"
echo "  tail -f \"$LOG_DIR/daemon.out.log\""
echo "  tail -f \"$LOG_DIR/daemon.err.log\""
echo
echo "Stop:"
echo "  launchctl unload \"$PLIST_DST\""
