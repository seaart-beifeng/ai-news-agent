#!/usr/bin/env bash
set -euo pipefail

LABEL="com.local.ai-news-agent"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ -f "$PLIST_DST" ]; then
  launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "Disabled $LABEL"
  echo "Removed $PLIST_DST"
else
  echo "$LABEL is not installed"
fi
