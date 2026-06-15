#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.local.ai-news-agent"
PLIST_SRC="$PROJECT_DIR/launchd/$LABEL.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl enable "$DOMAIN/$LABEL"
launchctl print "$DOMAIN/$LABEL" >/dev/null

echo "Enabled $LABEL"
echo "Schedule: every day at 10:00"
echo "Plist: $PLIST_DST"
echo "Logs:"
echo "  $PROJECT_DIR/logs/launchd.out.log"
echo "  $PROJECT_DIR/logs/launchd.err.log"
