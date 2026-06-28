#!/usr/bin/env bash
# Install a daily macOS schedule (launchd) that runs the summarizer locally.
# Re-run this any time to update it. Undo with: scripts/uninstall-local-schedule.sh
set -euo pipefail

cd "$(dirname "$0")/.."
repo="$(pwd)"
label="com.youtube-summary.local"
dest="$HOME/Library/LaunchAgents/$label.plist"

mkdir -p "$HOME/Library/LaunchAgents"
# Fill the repo path into the template and install it.
sed "s|__REPO__|$repo|g" "scripts/$label.plist" > "$dest"

chmod +x scripts/run_local.sh

# Reload if already installed.
launchctl unload "$dest" 2>/dev/null || true
launchctl load "$dest"

echo "Installed daily local run at 09:00 -> $dest"
echo "Logs: $repo/local-run.log"
echo "Run once now to test:  ./scripts/run_local.sh"
