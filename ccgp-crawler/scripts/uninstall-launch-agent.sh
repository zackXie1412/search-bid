#!/bin/zsh
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.lingxi.ccgp-console.plist"
if [[ -f "$PLIST_PATH" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
  mv "$PLIST_PATH" "$HOME/.Trash/com.lingxi.ccgp-console.plist.$(date +%s)"
  print "后台服务已停止，配置已移到废纸篓。"
else
  print "未安装后台服务。"
fi

