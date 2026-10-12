#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.lingxi.ccgp-console.plist"
LOG_DIR="$PROJECT_DIR/runtime/logs"
PYTHON_PATH="$PROJECT_DIR/.venv/bin/python"
INGEST_KEY_PATH="$PROJECT_DIR/runtime/ingest_api_key"

if [[ ! -x "$PYTHON_PATH" ]]; then
  print -u2 "未找到 $PYTHON_PATH，请先在项目目录运行 .venv/bin/python -m pip install -e ."
  exit 1
fi

if [[ -f "$INGEST_KEY_PATH" ]]; then
  chmod 600 "$INGEST_KEY_PATH"
else
  print -u2 "提示：未找到 $INGEST_KEY_PATH，工作台可启动，但云端上传将显示为未配置。"
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"
sed \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__PYTHON_PATH__|$PYTHON_PATH|g" \
  "$PROJECT_DIR/launchd/com.lingxi.ccgp-console.plist.template" > "$PLIST_PATH"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/com.lingxi.ccgp-console"
print "已安装并启动：$PLIST_PATH"
print "控制台地址：http://127.0.0.1:8765"
