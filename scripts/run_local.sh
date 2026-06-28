#!/usr/bin/env bash
# Run the summarizer on THIS machine (your residential IP), so YouTube — which
# blocks cloud/datacenter IPs like GitHub Actions — works for free. Pulls the
# latest state, runs, then commits & pushes the results back to the repo.
#
# One-off:   ./scripts/run_local.sh
# Scheduled: see scripts/com.youtube-summary.local.plist (macOS) or use cron.
set -euo pipefail

# Move to the repo root (this script lives in repo/scripts/).
cd "$(dirname "$0")/.."

branch="$(git rev-parse --abbrev-ref HEAD)"

# Load secrets/config from .env if present (ANTHROPIC/OPENAI/Deepgram keys, etc.)
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# Get the latest committed state so we don't redo items Actions already did.
git pull --rebase --autostash origin "$branch" || true

# Run. Pass through any extra args (e.g. --backfill 1).
python run.py "$@"

# Commit and push whatever changed (summaries, transcripts, state, index).
git add state.json summaries transcripts SUMMARIES.md 2>/dev/null || true
if ! git diff --cached --quiet; then
  git commit -m "Local run: update summaries [skip ci]"
  git push origin "$branch"
  echo "Pushed updated summaries."
else
  echo "Nothing new to commit."
fi
