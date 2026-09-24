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
