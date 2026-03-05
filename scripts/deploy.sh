#!/usr/bin/env sh
set -eu

log() {
  printf '[deploy] %s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '[deploy] 缺少命令: %s\n' "$1" >&2
    exit 1
  fi
}

require_var() {
  var_name="$1"
  eval "var_value=\${$var_name:-}"
  if [ -z "$var_value" ]; then
    printf '[deploy] 缺少环境变量: %s\n' "$var_name" >&2
    printf '[deploy] 请在本地设置，或写入: %s\n' "$DEPLOY_ENV_FILE" >&2
    exit 1
  fi
}

# 关键信息建议保存在本地环境文件，不提交到仓库。
# 示例文件（本地）：~/.config/grok2api/deploy.env
#   REMOTE_HOST="root@example.com"
#   REMOTE_NGINX_SITE="/etc/nginx/sites-enabled/example.conf"
#   REMOTE_APP_DIR="/opt/grok2api"
#   REMOTE_PERSIST_ROOT="/opt/grok2api_persist"
#   SSH_OPTS="-i ~/.ssh/id_rsa -p 22"
DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-$HOME/.config/grok2api/deploy.env}"
if [ -f "$DEPLOY_ENV_FILE" ]; then
  log "加载本地环境文件: $DEPLOY_ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  . "$DEPLOY_ENV_FILE"
  set +a
fi

REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_NGINX_SITE="${REMOTE_NGINX_SITE:-}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/grok2api}"
REMOTE_PERSIST_ROOT="${REMOTE_PERSIST_ROOT:-/opt/grok2api_persist}"
IMAGE_NAME="${IMAGE_NAME:-grok2api:latest}"
CONTAINER_PREFIX="${CONTAINER_PREFIX:-grok2api}"
SSH_OPTS="${SSH_OPTS:-}"

require_var REMOTE_HOST
require_var REMOTE_NGINX_SITE
require_cmd ssh

ssh_remote() {
  if [ -n "$SSH_OPTS" ]; then
    ssh $SSH_OPTS "$REMOTE_HOST" "$@"
  else
    ssh "$REMOTE_HOST" "$@"
  fi
}

sync_with_rsync() {
  log "使用 rsync 同步代码到 ${REMOTE_HOST}:${REMOTE_APP_DIR}"
  if [ -n "$SSH_OPTS" ]; then
    RSYNC_RSH="ssh $SSH_OPTS"
  else
    RSYNC_RSH="ssh"
  fi

  rsync -az --delete -e "$RSYNC_RSH" \
    --exclude '.git' \
    --exclude '.github' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.python-version' \
    --exclude 'logs/*' \
    --exclude 'data/*' \
    ./ "${REMOTE_HOST}:${REMOTE_APP_DIR}/"
}

sync_with_tar() {
  log "远程缺少 rsync，回退为 tar 流式同步"
  tar -cf - \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.python-version' \
    --exclude='logs/*' \
    --exclude='data/*' \
    . | ssh_remote "mkdir -p '$REMOTE_APP_DIR' && tar -xf - -C '$REMOTE_APP_DIR'"
}

if command -v rsync >/dev/null 2>&1 && ssh_remote "command -v rsync >/dev/null 2>&1"; then
  sync_with_rsync
else
  sync_with_tar
fi

log "执行远程部署"
if [ -n "$SSH_OPTS" ]; then
  # shellcheck disable=SC2086
  set -- ssh $SSH_OPTS "$REMOTE_HOST"
else
  set -- ssh "$REMOTE_HOST"
fi
"$@" "REMOTE_APP_DIR='$REMOTE_APP_DIR' REMOTE_PERSIST_ROOT='$REMOTE_PERSIST_ROOT' REMOTE_NGINX_SITE='$REMOTE_NGINX_SITE' IMAGE_NAME='$IMAGE_NAME' CONTAINER_PREFIX='$CONTAINER_PREFIX' sh -s" <<'REMOTE_EOF'
set -eu

log() {
  printf '[remote] %s\n' "$*"
}

has_published_port() {
  port="$1"
  docker ps -q --filter "publish=${port}" | grep -q '.'
}

container_id_by_port() {
  port="$1"
  docker ps -q --filter "publish=${port}" | head -n 1 || true
}

container_data_mount() {
  cid="$1"
  if [ -z "$cid" ]; then
    return 0
  fi
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' "$cid" 2>/dev/null || true
}

read_nginx_port() {
  if [ -f "$REMOTE_NGINX_SITE" ]; then
    sed -n 's/.*:\(8000\|8001\).*/\1/p' "$REMOTE_NGINX_SITE" | head -n 1 || true
  fi
}

http_check() {
  url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$url" >/dev/null 2>&1
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null "$url" >/dev/null 2>&1
    return $?
  fi
  return 0
}

CURRENT_PORT="$(read_nginx_port || true)"
if [ "$CURRENT_PORT" = "8000" ]; then
  TARGET_PORT="8001"
elif [ "$CURRENT_PORT" = "8001" ]; then
  TARGET_PORT="8000"
elif has_published_port 8000; then
  TARGET_PORT="8001"
elif has_published_port 8001; then
  TARGET_PORT="8000"
else
  TARGET_PORT="8000"
fi

CONTAINER_NAME="${CONTAINER_PREFIX}-${TARGET_PORT}"
PERSIST_DIR="${REMOTE_PERSIST_ROOT}/${TARGET_PORT}"
DATA_DIR_HOST="${PERSIST_DIR}/data"
LOG_DIR_HOST="${PERSIST_DIR}/logs"
RUNTIME_ENV_FILE="${PERSIST_DIR}/runtime.env"
SOURCE_PORT=""
SOURCE_PERSIST_DIR=""
SOURCE_CONTAINER_ID=""
SOURCE_CONTAINER_DATA_DIR=""
log "检测到当前端口: ${CURRENT_PORT:-未知}，目标端口: ${TARGET_PORT}"
log "持久化目录: ${PERSIST_DIR}"

if [ "$CURRENT_PORT" = "8000" ] || [ "$CURRENT_PORT" = "8001" ]; then
  SOURCE_PORT="$CURRENT_PORT"
else
  if [ "$TARGET_PORT" = "8000" ] && [ -d "${REMOTE_PERSIST_ROOT}/8001" ]; then
    SOURCE_PORT="8001"
  elif [ "$TARGET_PORT" = "8001" ] && [ -d "${REMOTE_PERSIST_ROOT}/8000" ]; then
    SOURCE_PORT="8000"
  fi
fi
SOURCE_PERSIST_DIR="${REMOTE_PERSIST_ROOT}/${SOURCE_PORT}"
SOURCE_CONTAINER_ID="$(container_id_by_port "$SOURCE_PORT")"
SOURCE_CONTAINER_DATA_DIR="$(container_data_mount "$SOURCE_CONTAINER_ID")"

TARGET_PORT_IDS="$(docker ps -aq --filter "publish=${TARGET_PORT}")"
if [ -n "$TARGET_PORT_IDS" ]; then
  log "删除占用 ${TARGET_PORT} 端口的旧容器"
  docker rm -f $TARGET_PORT_IDS
fi

if docker ps -aq --filter "name=^/${CONTAINER_NAME}$" | grep -q '.'; then
  log "删除同名容器 ${CONTAINER_NAME}"
  docker rm -f "$CONTAINER_NAME"
fi

mkdir -p "$DATA_DIR_HOST" "$LOG_DIR_HOST"

if [ -n "$SOURCE_PORT" ] && [ "$SOURCE_PORT" != "$TARGET_PORT" ]; then
  COPIED=0
  if [ -d "$SOURCE_PERSIST_DIR" ]; then
    log "复制上一版本隔离配置: ${SOURCE_PORT} -> ${TARGET_PORT}"
    if [ -f "${SOURCE_PERSIST_DIR}/runtime.env" ]; then
      cp -f "${SOURCE_PERSIST_DIR}/runtime.env" "$RUNTIME_ENV_FILE"
    fi
    if [ -f "${SOURCE_PERSIST_DIR}/data/config.toml" ]; then
      cp -f "${SOURCE_PERSIST_DIR}/data/config.toml" "${DATA_DIR_HOST}/config.toml"
      COPIED=1
    fi
    if [ -f "${SOURCE_PERSIST_DIR}/data/token.json" ]; then
      cp -f "${SOURCE_PERSIST_DIR}/data/token.json" "${DATA_DIR_HOST}/token.json"
      COPIED=1
    fi
  fi

  if [ "$COPIED" -eq 0 ] && [ -n "$SOURCE_CONTAINER_DATA_DIR" ] && [ -d "$SOURCE_CONTAINER_DATA_DIR" ]; then
    log "复制旧容器配置目录: ${SOURCE_CONTAINER_DATA_DIR} -> ${DATA_DIR_HOST}"
    if [ -f "${SOURCE_CONTAINER_DATA_DIR}/config.toml" ]; then
      cp -f "${SOURCE_CONTAINER_DATA_DIR}/config.toml" "${DATA_DIR_HOST}/config.toml"
      COPIED=1
    fi
    if [ -f "${SOURCE_CONTAINER_DATA_DIR}/token.json" ]; then
      cp -f "${SOURCE_CONTAINER_DATA_DIR}/token.json" "${DATA_DIR_HOST}/token.json"
      COPIED=1
    fi
  fi

  if [ "$COPIED" -eq 0 ]; then
    log "未找到可复制的历史配置，保留目标端口现有配置"
  fi
else
  log "跳过配置复制（无可用来源或端口一致）"
fi

log "构建镜像 ${IMAGE_NAME}"
docker build -t "$IMAGE_NAME" "$REMOTE_APP_DIR"

log "启动容器 ${CONTAINER_NAME}"
if [ -f "$RUNTIME_ENV_FILE" ]; then
  log "加载端口环境文件: ${RUNTIME_ENV_FILE}"
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "${TARGET_PORT}:8000" \
    --env-file "$RUNTIME_ENV_FILE" \
    -e TZ=Asia/Shanghai \
    -e SERVER_PORT=8000 \
    -e DATA_DIR=/app/data \
    -e LOG_DIR=/app/logs \
    -v "${DATA_DIR_HOST}:/app/data" \
    -v "${LOG_DIR_HOST}:/app/logs" \
    "$IMAGE_NAME" >/dev/null
else
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "${TARGET_PORT}:8000" \
    -e TZ=Asia/Shanghai \
    -e SERVER_PORT=8000 \
    -e DATA_DIR=/app/data \
    -e LOG_DIR=/app/logs \
    -v "${DATA_DIR_HOST}:/app/data" \
    -v "${LOG_DIR_HOST}:/app/logs" \
    "$IMAGE_NAME" >/dev/null
fi

log "等待服务启动"
ATTEMPT=0
until http_check "http://127.0.0.1:${TARGET_PORT}/admin"; do
  ATTEMPT=$((ATTEMPT + 1))
  if [ "$ATTEMPT" -ge 30 ]; then
    log "新容器健康检查失败，输出最近日志"
    docker logs --tail 100 "$CONTAINER_NAME" >&2 || true
    exit 1
  fi
  sleep 2
done

if [ ! -f "$REMOTE_NGINX_SITE" ]; then
  printf '[remote] Nginx 配置不存在: %s\n' "$REMOTE_NGINX_SITE" >&2
  exit 1
fi

log "更新 Nginx 配置到 ${TARGET_PORT}"
sed -i -E "s#(proxy_pass[[:space:]]+http://(127\\.0\\.0\\.1|localhost):)(8000|8001)([;/])#\\1${TARGET_PORT}\\4#g" "$REMOTE_NGINX_SITE"
sed -i -E "s#(127\\.0\\.0\\.1:|localhost:)(8000|8001)#\\1${TARGET_PORT}#g" "$REMOTE_NGINX_SITE"

nginx -t
if command -v systemctl >/dev/null 2>&1; then
  systemctl reload nginx
else
  nginx -s reload
fi

printf '%s\n' "$TARGET_PORT" > "${REMOTE_APP_DIR}/.active_port"
log "部署完成，当前生效端口: ${TARGET_PORT}"
REMOTE_EOF

log "本地一键部署完成"
