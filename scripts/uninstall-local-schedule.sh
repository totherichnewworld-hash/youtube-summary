#!/usr/bin/env bash
# Remove the daily local schedule installed by install-local-schedule.sh.
set -euo pipefail
label="com.youtube-summary.local"
dest="$HOME/Library/LaunchAgents/$label.plist"
launchctl unload "$dest" 2>/dev/null || true
rm -f "$dest"
echo "Removed local schedule ($label)."
