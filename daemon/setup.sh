#!/usr/bin/env bash
# One-shot setup for the podcast Mac daemon:
#   - installs espeak-ng (via Homebrew)
#   - creates a venv and installs Python deps
#   - downloads Kokoro model files (~340 MB)
#
# This does NOT install the LaunchAgent. Run `install-agent.sh` for that,
# typically after the iOS app has been installed and the iCloud folder appears.
set -euo pipefail

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_DIR="$DAEMON_DIR/models"
VENV_DIR="$DAEMON_DIR/.venv"

echo "==> daemon dir: $DAEMON_DIR"

# 1. espeak-ng (Kokoro phonemizer)
if ! command -v espeak-ng >/dev/null 2>&1; then
  echo "==> installing espeak-ng via Homebrew"
  if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew not found. Install from https://brew.sh first." >&2
    exit 1
  fi
  brew install espeak-ng
else
  echo "==> espeak-ng already installed"
fi

# 2. Python venv + deps (via uv, which kokoro-onnx 0.5+ needs 3.10+)
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi
if [ ! -d "$VENV_DIR" ]; then
  echo "==> creating venv at $VENV_DIR (Python 3.11)"
  uv venv --python 3.11 "$VENV_DIR"
fi
echo "==> installing Python deps"
VIRTUAL_ENV="$VENV_DIR" uv pip install --quiet -r "$DAEMON_DIR/requirements.txt"

# 3. Kokoro model files
mkdir -p "$MODELS_DIR"
MODEL_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

if [ ! -f "$MODELS_DIR/kokoro-v1.0.onnx" ]; then
  echo "==> downloading kokoro-v1.0.onnx (~310 MB)"
  curl -L --fail --progress-bar -o "$MODELS_DIR/kokoro-v1.0.onnx" "$MODEL_URL"
else
  echo "==> kokoro-v1.0.onnx already present"
fi

if [ ! -f "$MODELS_DIR/voices-v1.0.bin" ]; then
  echo "==> downloading voices-v1.0.bin (~26 MB)"
  curl -L --fail --progress-bar -o "$MODELS_DIR/voices-v1.0.bin" "$VOICES_URL"
else
  echo "==> voices-v1.0.bin already present"
fi

echo
echo "==> done. Next steps:"
echo "  1. Run 'claude /login' once if you haven't (in any terminal)"
echo "  2. Try a manual end-to-end run:"
echo "       PODCAST_INBOX=/tmp/podcast-test $VENV_DIR/bin/python3 $DAEMON_DIR/daemon.py"
echo "     ...and in another terminal drop a request file in /tmp/podcast-test"
echo "  3. Once the iOS app is installed and the iCloud folder shows up,"
echo "     run install-agent.sh to start the daemon at login."
