#!/usr/bin/env bash
# Деплой с една команда, с проверки. Идемпотентно: второ пускане не променя
# нищо и минава всички проверки.
#   sudo GENESIS_DOMAIN=genesis.example.com ./cloud/deploy.sh
#   DEPLOY_DRY_RUN=1 ./cloud/deploy.sh        # само показва стъпките
# Ред: проверки → git pull → образи → daemon.json → up.sh → systemd → Caddy →
# cloud/check.py → https://<домейн>/healthz. Спира на първата грешка.
# Променливи: GENESIS_DOMAIN (без него — без Caddy), GENESIS_KEYS
# (/srv/genesis/keys.env), GENESIS_RUNTIME (напр. runsc за gVisor),
# GENESIS_NO_PULL=1, DEPLOY_DRY_DIR (къде сухото пускане пише файловете).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
DRY="${DEPLOY_DRY_RUN:-0}"
DOMAIN="${GENESIS_DOMAIN:-}"
KEYS="${GENESIS_KEYS:-/srv/genesis/keys.env}"
USAGE="${GENESIS_GATEWAY_DATA:-/srv/genesis/gateway}/usage.jsonl"
DOCKER="${DOCKER:-docker}"
PYTHON="${PYTHON:-python3}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
export DOCKER PYTHON

step() { printf '\n── %s\n' "$*"; }
fail() { echo "ГРЕШКА: $*" >&2; exit 1; }
# Сухо пускане: командите само се показват; проверките преди деплой се
# изпълняват (само четат), но грешката им е предупреждение.
run() { if [ "$DRY" = 1 ]; then echo "  [сухо] $*"; else "$@"; fi; }
need() { if "$@" >/dev/null 2>&1; then return 0; fi
         if [ "$DRY" = 1 ]; then echo "  [сухо] не минава: $*"; else return 1; fi; }
if [ "$DRY" = 1 ]; then
  OUT="${DEPLOY_DRY_DIR:-$(mktemp -d)}"
  mkdir -p "$OUT"
fi
# Шаблонът с @REPO@/@DOMAIN@ → целевия файл (в сухо пускане — в $OUT).
install_file() {  # шаблон цел
  local tmp
  tmp="$(mktemp)"
  sed -e "s#@REPO@#$REPO#g" -e "s#@DOMAIN@#$DOMAIN#g" "$1" > "$tmp"
  if [ "$DRY" = 1 ]; then
    mv "$tmp" "$OUT/$(basename "$2")"
    echo "  [сухо] $2 ← $OUT/$(basename "$2")"
  else
    install -m 0644 "$tmp" "$2"
    rm -f "$tmp"
  fi
}

step "проверки преди деплой"
need test "$(id -u)" = 0 || fail "пусни като root (sudo): вика docker, systemctl и пише в /etc"
# Ревю 2026-10-11: genesis-web и genesis-backup вървят като root с кода от тази
# папка — файл, който друг потребител може да пише, е root за него. И git като
# root отказва чужда папка („dubious ownership“, git ≥ 2.35.2): `git pull`
# спираше деплоя на „код“, а build.sh слагаше :dev вместо :<sha>.
BAD="$(find "$REPO" \( ! -user 0 -o \( -perm -0002 ! -type l \) \) -print -quit 2>/dev/null || true)"
need test -z "$BAD" \
  || fail "$BAD не е само на root — кодът трябва да е на root: sudo chown -R root:root $REPO && sudo chmod -R o-w $REPO"
need "$DOCKER" info || fail "Docker не отговаря (apt install docker.io или docker-ce)"
need "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)' \
  || fail "нужен е python3 ≥ 3.10 и python3-pip (apt install python3 python3-pip)"
need "$PYTHON" -m pip --version || fail "няма pip (apt install python3-pip) — build.sh тегли wheels с него"
[ -f /sys/fs/cgroup/cgroup.controllers ] || echo "  внимание: cgroup v1 — работи, но Docker го обявява за остарял"
MODE=proxy
if [ -f "$KEYS" ] && grep -Eq '^[[:space:]]*GATEWAY_SECRET[[:space:]]*=' "$KEYS"; then
  MODE=gateway
  need "$PYTHON" -c 'import sys; from pathlib import Path; from cloud.gateway.gateway import read_keys; sys.exit(len(read_keys(Path(sys.argv[1])).get("GATEWAY_SECRET", "")) < 32)' "$KEYS" \
    || fail "GATEWAY_SECRET в $KEYS е под 32 знака (openssl rand -base64 48)"
  [ "$(stat -c %a "$KEYS")" != 640 ] && [ "$(stat -c %a "$KEYS")" != 600 ] \
    && fail "$KEYS трябва да е 600 или 640, а е $(stat -c %a "$KEYS")"
elif [ -f "$KEYS" ]; then
  echo "  внимание: в $KEYS няма GATEWAY_SECRET — ключовете ще влизат в контейнерите (режим с прокси)"
else
  echo "  внимание: няма $KEYS — задачите няма да имат модел"
fi
echo "  режим: $MODE"
AVAIL_KB="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}')"
FREE_GB=$(( ${AVAIL_KB:-0} / 1048576 ))
[ "$FREE_GB" -ge 10 ] || echo "  внимание: само ${FREE_GB} GB свободни (образите са ~1 GB, задачите пишат в /srv/jobs)"
if [ -n "$DOMAIN" ]; then
  need command -v caddy || fail "няма Caddy (apt install caddy) — нужен е за HTTPS пред $DOMAIN"
fi

step "код"
if [ "${GENESIS_NO_PULL:-0}" = 1 ]; then echo "  пропуснато (GENESIS_NO_PULL=1)"; else run git pull --ff-only; fi

step "образи (cloud/build.sh)"
run bash "$REPO/cloud/build.sh"

step "Docker: таван на логовете и малки мрежи (/etc/docker/daemon.json)"
if [ -f /etc/docker/daemon.json ]; then
  # Не се пипа чужд файл: само се казва какво липсва.
  grep -q max-size /etc/docker/daemon.json || echo "  внимание: в daemon.json няма log-opts max-size — виж cloud/deploy/daemon.json"
  grep -q default-address-pools /etc/docker/daemon.json \
    || echo "  внимание: няма default-address-pools — по подразбиране има място за ~30 мрежи на задачи"
else
  install_file "$REPO/cloud/deploy/daemon.json" /etc/docker/daemon.json
  run "$SYSTEMCTL" restart docker
fi

step "мрежи и помощник (cloud/up.sh)"
run bash "$REPO/cloud/up.sh"

step "systemd"
ARGS=""
if [ "$MODE" = gateway ]; then ARGS="--env-file $KEYS --gateway-usage $USAGE"
elif [ -f "$KEYS" ]; then ARGS="--env-file $KEYS"; fi
[ -n "${GENESIS_RUNTIME:-}" ] && ARGS="$ARGS --runtime $GENESIS_RUNTIME"
DEFAULTS="$(mktemp)"
printf '# cloud/deploy.sh — режим %s\nGENESIS_WEB_ARGS=%s\n' "$MODE" "$ARGS" > "$DEFAULTS"
install_file "$DEFAULTS" /etc/default/genesis-web
rm -f "$DEFAULTS"
for unit in genesis-net.service genesis-web.service genesis-backup.service genesis-backup.timer; do
  install_file "$REPO/cloud/deploy/$unit" "/etc/systemd/system/$unit"
done
run mkdir -p /srv/genesis/web /srv/jobs /srv/backup/genesis
run chmod 700 /srv/genesis/web /srv/backup/genesis
run "$SYSTEMCTL" daemon-reload
run "$SYSTEMCTL" enable genesis-net.service genesis-web.service genesis-backup.timer
run "$SYSTEMCTL" restart genesis-net.service
# Рестартът спира текущите задачи (сървърът ги убива при SIGTERM).
run "$SYSTEMCTL" restart genesis-web.service
run "$SYSTEMCTL" start genesis-backup.timer

if [ -n "$DOMAIN" ]; then
  step "Caddy (HTTPS за $DOMAIN)"
  install_file "$REPO/cloud/deploy/Caddyfile" /etc/caddy/Caddyfile
  run caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  run "$SYSTEMCTL" reload-or-restart caddy
fi

step "проверка (cloud/check.py)"
run sleep 2
run "$PYTHON" -m cloud.check --web http://127.0.0.1:8080
if [ -n "$DOMAIN" ]; then run curl -fsS "https://$DOMAIN/healthz"; fi

step "готово"
echo "  защитна стена: отвори само 22, 80, 443 (ufw allow OpenSSH; ufw allow 80,443/tcp; ufw enable)"
echo "  Docker не публикува портове, затова заобикалянето на ufw от Docker тук не важи."
