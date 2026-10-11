#!/usr/bin/env bash
# Вдига мрежите и помощниците на задачите. Идемпотентно — безопасно да се пуска
# повторно (и от systemd: deploy/genesis-net.service).
#   genesis-outside — обикновен bridge; помощниците излизат навън през него
#   genesis-jobs    — --internal, за ръчните проверки (README); задачите от
#                     launch.py са всяка в своя мрежа genesis-job-<id>
# Режимът идва от keys.env:
#   има GATEWAY_SECRET → шлюз (ключовете остават в genesis-gateway); проксито
#                        НЕ се пуска — иначе задача стигаше до API-тата покрай
#                        шлюза със свой ключ (деплой одит 2026-10-11);
#   иначе              → прокси (genesis-egress), шлюзът се маха.
# Свършва с код ≠ 0, ако пуснатото не е живо и здраво след няколко секунди.
set -euo pipefail
DOCKER="${DOCKER:-docker}"
KEYS="${GENESIS_KEYS:-/srv/genesis/keys.env}"
USAGE_DIR="${GENESIS_GATEWAY_DATA:-/srv/genesis/gateway}"
WAIT="${GENESIS_UP_WAIT:-5}"
# Логовете на помощниците са с таван: проксито логва всеки отказ и json-file
# без max-size растеше без край (одит 2026-10-11: LogConfig {json-file map[]}).
LOGS=(--log-opt max-size=10m --log-opt max-file=5)
HARDEN=(--read-only --cap-drop ALL --security-opt no-new-privileges
        --memory 256m --pids-limit 128 --restart unless-stopped)

"$DOCKER" network inspect genesis-jobs >/dev/null 2>&1 || "$DOCKER" network create --internal genesis-jobs >/dev/null
"$DOCKER" network inspect genesis-outside >/dev/null 2>&1 || "$DOCKER" network create genesis-outside >/dev/null

# Жив ли е контейнерът и не се ли рестартира. Одит 2026-10-11: up.sh печаташе
# „готово“, а шлюзът беше в restarting (GATEWAY_SECRET под 32 знака).
healthy() {  # име
  local state
  sleep "$WAIT"
  state="$("$DOCKER" inspect -f '{{.State.Running}} {{.RestartCount}}' "$1" 2>/dev/null || true)"
  if [ "$state" != "true 0" ]; then
    echo "ГРЕШКА: $1 не работи (Running RestartCount = '${state:-няма го}'). Последните редове:" >&2
    "$DOCKER" logs --tail 20 "$1" >&2 || true
    return 1
  fi
}

# Отговаря ли помощникът оттам, откъдето го викат задачите (вътрешна мрежа).
probe() {  # образ, код на python
  "$DOCKER" run --rm --network genesis-jobs --read-only --cap-drop ALL \
    --entrypoint python "$1" -c "$2" >/dev/null
}

if [ -f "$KEYS" ] && grep -Eq '^[[:space:]]*GATEWAY_SECRET[[:space:]]*=' "$KEYS"; then
  "$DOCKER" rm -f genesis-egress >/dev/null 2>&1 || true
  mkdir -p "$USAGE_DIR"
  if [ "$(id -u)" = 0 ]; then
    # Шлюзът (uid/gid 10003) чете ключовете през групата, уеб сървърът (root —
    # вика docker и дава папките на задачите на uid 10001) — като собственик.
    # Одит 2026-10-11: `chown 10003` оставяше keys.env 10003:root 600 и уеб
    # сървърът (--env-file/--gateway-usage) падаше с PermissionError.
    chown 0:10003 "$KEYS" && chmod 640 "$KEYS"
    chown 10003:10003 "$USAGE_DIR"
  else
    echo "внимание: не съм root — keys.env трябва да се чете от gid 10003, $USAGE_DIR да се пише от uid 10003" >&2
  fi
  "$DOCKER" rm -f genesis-gateway >/dev/null 2>&1 || true
  "$DOCKER" run -d --name genesis-gateway --network genesis-outside "${HARDEN[@]}" "${LOGS[@]}" \
    -v "$KEYS:/keys.env:ro" -v "$USAGE_DIR:/data" \
    genesis-gateway:latest --keys /keys.env --usage /data/usage.jsonl >/dev/null
  "$DOCKER" network connect genesis-jobs genesis-gateway
  healthy genesis-gateway
  probe genesis-gateway:latest "import urllib.request as u; u.urlopen('http://genesis-gateway:8090/healthz', timeout=5)" \
    || { echo "ГРЕШКА: genesis-gateway не отговаря на /healthz от genesis-jobs" >&2; exit 1; }
  echo "готово: genesis-gateway слуша на :8090 (режим с шлюз, без прокси)"
else
  if [ -f "$KEYS" ]; then
    echo "в $KEYS няма GATEWAY_SECRET — режим с прокси (ключовете влизат в контейнера)"
  else
    echo "няма $KEYS — режим с прокси"
  fi
  # Стар шлюз от предишно пускане не остава да върви с изтрити ключове.
  "$DOCKER" rm -f genesis-gateway >/dev/null 2>&1 || true
  "$DOCKER" rm -f genesis-egress >/dev/null 2>&1 || true
  "$DOCKER" run -d --name genesis-egress --network genesis-outside "${HARDEN[@]}" "${LOGS[@]}" \
    genesis-egress:latest >/dev/null
  "$DOCKER" network connect genesis-jobs genesis-egress
  healthy genesis-egress
  # Проксито отказва всичко извън API-тата на моделите: CONNECT към example.com → 403.
  probe genesis-egress:latest "
import socket
s = socket.create_connection(('genesis-egress', 3128), timeout=5)
s.sendall(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n')
assert s.recv(64).startswith(b'HTTP/1.1 403'), 'проксито не отказа example.com'
" || { echo "ГРЕШКА: genesis-egress не отказва example.com с 403" >&2; exit 1; }
  echo "готово: genesis-egress слуша на :3128 (режим с прокси)"
fi
