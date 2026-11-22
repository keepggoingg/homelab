#!/bin/sh
# Install homelab-sync on a Mac: calendars, reminders and chosen notes -> the homelab, every 5 minutes.
#
#   clients/macos/install.sh http://<HL_HOST>:8090
#
# Asks for the push token (PUSH_TOKEN in config/homelab.env on the server; or pipe it in on stdin),
# keeps it in the Keychain, builds the tool, and loads a launchd agent. macOS asks once to allow
# Calendars, Reminders and controlling Notes: click Allow.
# Uninstall: clients/macos/install.sh --uninstall
set -eu
LABEL=dev.homelab.sync
BIN="$HOME/.local/bin/homelab-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$BIN"; rm -rf "$HOME/.config/homelab-sync"
  security delete-generic-password -s homelab-sync >/dev/null 2>&1 || true
  echo "homelab-sync removed"; exit 0
fi
URL=${1:?usage: install.sh http://<host>:8090}
URL=${URL%/}

command -v swiftc >/dev/null || { echo "needs the Xcode command line tools: xcode-select --install"; exit 1; }
curl -sf -o /dev/null "$URL/api/config" || { echo "can't reach $URL/api/config (LAN / Tailscale?)"; exit 1; }

# build with an embedded Info.plist, so macOS asks permission for this tool (not for your terminal)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>homelab-sync</string>
  <key>NSCalendarsFullAccessUsageDescription</key><string>Shows your upcoming events on the homelab dashboard.</string>
  <key>NSRemindersFullAccessUsageDescription</key><string>Shows your open reminders on the homelab dashboard.</string>
  <key>NSAppleEventsUsageDescription</key><string>Reads the Apple Notes you chose for the homelab dashboard.</string>
</dict></plist>
EOF
mkdir -p "$(dirname "$BIN")" "$HOME/.config/homelab-sync" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
swiftc -O "$HERE/homelab-sync.swift" -o "$TMP/homelab-sync" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$TMP/Info.plist"
codesign -s - -f -i "$LABEL" "$TMP/homelab-sync" 2>/dev/null
mv "$TMP/homelab-sync" "$BIN"
printf '%s\n' "$URL" > "$HOME/.config/homelab-sync/url"

if [ -t 0 ]; then
  printf 'Push token (PUSH_TOKEN from config/homelab.env on the server): '
  stty -echo; read -r TOKEN; stty echo; echo
  printf '%s' "$TOKEN" | "$BIN" --store-token
else
  "$BIN" --store-token          # token piped in on stdin
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/homelab-sync.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/homelab-sync.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: runs every 5 minutes, log in ~/Library/Logs/homelab-sync.log"
echo "macOS will ask to allow Calendars, Reminders and Notes for homelab-sync: click Allow."
