#!/bin/zsh
# Double-click this file in Finder: it pulls new activities from Strava,
# rebuilds map.html, and opens the map in your default browser.
cd "$(dirname "$0")" || exit 1
echo "Syncing Strava..."
if ! .venv/bin/python strava_sync.py; then
  echo
  echo "Sync failed. See the error above."
fi
echo
echo "Press Return to close this window."
read -r
