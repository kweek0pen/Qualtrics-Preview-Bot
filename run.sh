#!/bin/zsh
# One command for the whole preview batch:
#
#     ./run.sh                # 12 runs, default preview link
#     ./run.sh --runs 1       # any safari_runner.py flag passes through
#
# It sets up ./venv and the Python packages, loads the OpenAI key and the
# preview link from .env (asking once for anything missing), keeps the Mac
# from sleeping, opens the preview in its own Safari window, and writes
# everything to runs/<timestamp>/.

set -e
cd "${0:A:h}"

PY=./venv/bin/python
if [[ ! -x $PY ]]; then
  echo "Creating a Python virtual environment in ./venv (one time)..."
  python3 -m venv venv || { echo "Needs Python 3.10+: install it from python.org"; exit 1; }
fi

if ! $PY -c "import playwright, openai, openpyxl" 2>/dev/null; then
  echo "Installing Python packages..."
  $PY -m pip install -q -r requirements.txt
fi
if ! $PY - <<'EOF' 2>/dev/null
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch(headless=True).close()
EOF
then
  echo "Installing the headless browser for the chat (one time)..."
  $PY -m playwright install chromium
fi

# The key and the preview link live in .env (git-ignored), never in the scripts.
if [[ -f .env ]]; then
  set -a; source ./.env; set +a
fi
if [[ -z $OPENAI_API_KEY ]]; then
  echo -n "OpenAI API key (input hidden): "
  read -rs OPENAI_API_KEY; echo
  [[ -z $OPENAI_API_KEY ]] && { echo "No key given."; exit 1; }
  export OPENAI_API_KEY
  echo -n "Save it to .env in this folder for next time? [y/N] "
  read -r save
  if [[ $save == [yY]* ]]; then
    umask 077; print -r -- "OPENAI_API_KEY=$OPENAI_API_KEY" > .env
    echo "Saved to .env (readable only by you)."
  fi
fi

# Skip the prompt when the link is passed on the command line.
if [[ -z $QUALTRICS_PREVIEW_URL && " $* " != *" --preview-url "* ]]; then
  echo "Paste your Qualtrics preview link (from Preview in the survey editor):"
  read -r QUALTRICS_PREVIEW_URL
  [[ -z $QUALTRICS_PREVIEW_URL ]] && { echo "No preview link given."; exit 1; }
  export QUALTRICS_PREVIEW_URL
  echo -n "Save it to .env for next time? [y/N] "
  read -r save
  if [[ $save == [yY]* ]]; then
    umask 077; print -r -- "QUALTRICS_PREVIEW_URL='$QUALTRICS_PREVIEW_URL'" >> .env
    echo "Saved to .env."
  fi
fi

mkdir -p runs
# -i: no idle sleep while the batch runs; the display may still turn off.
caffeinate -i $PY -u safari_runner.py "$@" 2>&1 | tee runs/last_run.log
