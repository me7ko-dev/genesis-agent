#!/usr/bin/env bash
# Вдига мрежите и проксито. Идемпотентно — безопасно да се пуска повторно.
#   genesis-jobs    — --internal: БЕЗ изход навън; тук живеят задачите
#   genesis-outside — обикновен bridge; само проксито е и в двете
set -euo pipefail
docker network inspect genesis-jobs >/dev/null 2>&1 || docker network create --internal genesis-jobs
docker network inspect genesis-outside >/dev/null 2>&1 || docker network create genesis-outside
docker rm -f genesis-egress >/dev/null 2>&1 || true
docker run -d --name genesis-egress --restart unless-stopped \
  --network genesis-outside --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 256m --pids-limit 128 genesis-egress:latest
docker network connect genesis-jobs genesis-egress
echo "готово: genesis-egress слуша на genesis-jobs:3128"

# Шлюзът за моделите (cloud/gateway): ключовете остават тук, задачите получават
# жетон. В keys.env трябва да има GATEWAY_SECRET (поне 32 знака).
KEYS="${GENESIS_KEYS:-/srv/genesis/keys.env}"
USAGE_DIR="${GENESIS_GATEWAY_DATA:-/srv/genesis/gateway}"
if [ -f "$KEYS" ]; then
  mkdir -p "$USAGE_DIR"
  # потребител 10003 в образа трябва да чете ключовете и да пише разхода
  if [ "$(id -u)" = 0 ]; then chown 10003 "$KEYS" "$USAGE_DIR"; fi
  docker rm -f genesis-gateway >/dev/null 2>&1 || true
  docker run -d --name genesis-gateway --restart unless-stopped \
    --network genesis-outside --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 256m --pids-limit 128 \
    -v "$KEYS:/keys.env:ro" -v "$USAGE_DIR:/data" \
    genesis-gateway:latest --keys /keys.env --usage /data/usage.jsonl
  docker network connect genesis-jobs genesis-gateway
  echo "готово: genesis-gateway слуша на genesis-jobs:8090"
else
  echo "няма $KEYS — шлюзът не е пуснат (задачите ще са в режим с прокси)"
fi
