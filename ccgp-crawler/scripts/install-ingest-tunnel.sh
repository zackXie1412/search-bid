#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
SSH_KEY_PATH="$HOME/.ssh/id_ed25519"
SSH_USER="ubuntu"
SSH_HOST="175.178.172.158"
LOCAL_PORT="18765"
REMOTE_PORT="18080"

usage() {
  print "用法: $0 [--identity PATH] [--user USER] [--host HOST] [--local-port PORT] [--remote-port PORT]"
}

while (( $# > 0 )); do
  case "$1" in
    --identity) SSH_KEY_PATH="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --host) SSH_HOST="$2"; shift 2 ;;
    --local-port) LOCAL_PORT="$2"; shift 2 ;;
    --remote-port) REMOTE_PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) print -u2 "未知参数: $1"; usage >&2; exit 2 ;;
  esac
done

SSH_KEY_PATH=${SSH_KEY_PATH:A}
if [[ ! -f "$SSH_KEY_PATH" ]]; then
  print -u2 "未找到 SSH 私钥: $SSH_KEY_PATH"
  print -u2 "请先配置公钥登录，或通过 --identity 指定私钥。"
  exit 1
fi
if [[ ! "$SSH_USER" =~ '^[A-Za-z0-9._-]+$' || ! "$SSH_HOST" =~ '^[A-Za-z0-9.:-]+$' ]]; then
  print -u2 "SSH 用户名或主机名格式不合法。"
  exit 1
fi
if [[ ! "$LOCAL_PORT" =~ '^[0-9]+$' || ! "$REMOTE_PORT" =~ '^[0-9]+$' ]]; then
  print -u2 "端口必须是数字。"
  exit 1
fi

print "检查 SSH 公钥登录…"
/usr/bin/ssh \
  -i "$SSH_KEY_PATH" \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=accept-new \
  "$SSH_USER@$SSH_HOST" true

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.lingxi.ccgp-ingest-tunnel.plist"
LOG_DIR="$PROJECT_DIR/runtime/logs"
launchctl bootout "gui/$(id -u)/com.lingxi.ccgp-ingest-tunnel" 2>/dev/null || true

if /usr/sbin/lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  print -u2 "本地端口 $LOCAL_PORT 已被占用，未安装隧道。"
  exit 1
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"
escape_replacement() {
  print -r -- "$1" | sed 's/[|&\\]/\\&/g'
}
PROJECT_REPLACEMENT=$(escape_replacement "$PROJECT_DIR")
KEY_REPLACEMENT=$(escape_replacement "$SSH_KEY_PATH")
sed \
  -e "s|__PROJECT_DIR__|$PROJECT_REPLACEMENT|g" \
  -e "s|__SSH_KEY_PATH__|$KEY_REPLACEMENT|g" \
  -e "s|__SSH_USER__|$SSH_USER|g" \
  -e "s|__SSH_HOST__|$SSH_HOST|g" \
  -e "s|__LOCAL_PORT__|$LOCAL_PORT|g" \
  -e "s|__REMOTE_PORT__|$REMOTE_PORT|g" \
  "$PROJECT_DIR/launchd/com.lingxi.ccgp-ingest-tunnel.plist.template" > "$PLIST_PATH"

/usr/bin/plutil -lint "$PLIST_PATH" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/com.lingxi.ccgp-ingest-tunnel"
print "已安装并启动 SSH 隧道: 127.0.0.1:$LOCAL_PORT -> $SSH_HOST:$REMOTE_PORT"
print "可用 curl http://127.0.0.1:$LOCAL_PORT/healthz 验证连接。"
