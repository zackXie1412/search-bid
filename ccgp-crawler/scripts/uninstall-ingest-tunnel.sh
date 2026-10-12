#!/bin/zsh
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.lingxi.ccgp-ingest-tunnel.plist"
launchctl bootout "gui/$(id -u)/com.lingxi.ccgp-ingest-tunnel" 2>/dev/null || true
if [[ -f "$PLIST_PATH" ]]; then
  mkdir -p "$HOME/.Trash"
  mv "$PLIST_PATH" "$HOME/.Trash/com.lingxi.ccgp-ingest-tunnel.plist.$(date +%s)"
  print "SSH 隧道已停止，配置已移到废纸篓。"
else
  print "未安装 SSH 隧道。"
fi
