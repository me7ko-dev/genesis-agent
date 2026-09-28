#!/data/data/com.termux/files/usr/bin/bash
# Genesis на телефона (Android), без компютър. Пуска се в Termux:
#
#   curl -fsSL https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install-termux.sh | bash
#
# Слага python, git и node от Termux, Genesis в собствена venv
# (~/.genesis/venv) и командата `genesis`. Разрешава на приложението Genesis
# да го пуска (allow-external-apps), пуска го при включване на телефона
# (ако има Termux:Boot) и накрая отваря приложението, вече сдвоено.
# Пуснат пак, обновява Genesis до последната версия. Виж docs/ANDROID.md.
#
#   GENESIS_REF=<клон>   от кой клон на GitHub (по подразбиране main)
#   GENESIS_SRC=<път>    от локално копие вместо от GitHub (тестове)
#   GENESIS_CI=1         без въпроси: без ключове, без достъп до паметта, без приложението
set -euo pipefail

REF="${GENESIS_REF:-main}"
VENV="$HOME/.genesis/venv"
if [ -n "${GENESIS_SRC:-}" ]; then
  PKG="${GENESIS_SRC}[mobile]"
else
  PKG="genesis-agent[mobile] @ git+https://github.com/me7ko-dev/genesis-agent@${REF}"
fi

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$1" "$2"; }
die() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

if [ -z "${PREFIX:-}" ] || [ ! -x "$PREFIX/bin/pkg" ]; then
  die "Това е за Termux на Android. На компютъра виж docs/WINDOWS.md."
fi

step 1/6 "Пакети от Termux: python, git, node (първия път — няколко минути)"
# Termux обновява всичко наведнъж: нов пакет върху стара система пада с
# „CANNOT LINK EXECUTABLE". Въпросите на dpkg за конфигурации — без питане.
export DEBIAN_FRONTEND=noninteractive
APT=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
apt-get update
apt-get "${APT[@]}" upgrade
# python-cryptography е готов от Termux: през pip би се компилирал с Rust.
apt-get "${APT[@]}" install python python-pip python-cryptography git nodejs-lts termux-tools

step 2/6 "Genesis (в ~/.genesis/venv)"
# --system-site-packages: venv-ът вижда cryptography от Termux.
[ -x "$VENV/bin/python" ] || python -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade --disable-pip-version-check "$PKG"
# Номерът на версията не се сменя с всеки commit, а pip пропуска „същата“
# версия: без това пускането пак НЕ обновяваше (наживо, 2026-09-28).
"$VENV/bin/python" -m pip install --force-reinstall --no-deps --disable-pip-version-check "$PKG"
ln -sf "$VENV/bin/genesis" "$PREFIX/bin/genesis"
genesis --version

step 3/6 "Разрешение за приложението Genesis да пуска агента"
mkdir -p "$HOME/.termux"
PROPS="$HOME/.termux/termux.properties"
if ! grep -qsE '^[[:space:]]*allow-external-apps[[:space:]]*=[[:space:]]*true' "$PROPS"; then
  [ -f "$PROPS" ] && sed -i '/^[[:space:]]*#*[[:space:]]*allow-external-apps/d' "$PROPS"
  echo 'allow-external-apps = true' >> "$PROPS"
  termux-reload-settings 2>/dev/null || true
fi

step 4/6 "Пускане при включване на телефона (с приложението Termux:Boot)"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/genesis" <<EOF
#!$PREFIX/bin/sh
# Пуска Genesis при включване на телефона. Работи, ако е инсталирано
# приложението Termux:Boot (F-Droid) и е отворено поне веднъж.
termux-wake-lock
exec "$PREFIX/bin/genesis" phone start
EOF
chmod 700 "$HOME/.termux/boot/genesis"

if [ -n "${GENESIS_CI:-}" ]; then
  step 5/6 "CI: без ключове и без достъп до паметта"
  step 6/6 "CI: без приложението"
  exit 0
fi

step 5/6 "Ключове за моделите и достъп до паметта на телефона"
if [ ! -d "$HOME/storage" ]; then
  # Android пита веднъж: тогава агентът вижда ~/storage/downloads и т.н.
  termux-setup-storage || true
fi
KEYS_LATER=""
if [ -s "$HOME/.genesis/.env" ]; then
  echo "Ключовете вече са тук (~/.genesis/.env). Нови или други: genesis setup"
else
  echo "Ключовете за моделите:"
  echo "  Enter     — пренеси ги от компютъра (там: genesis keys qr, после сканирай от приложението)"
  echo "  р + Enter — напиши ги сега тук"
  # stdin е тръбата от curl — въпросите четат от терминала.
  read -r answer < /dev/tty || answer=""
  case "$answer" in
    р|Р|p|P) genesis setup < /dev/tty || echo "Ключовете — по-късно: genesis setup" ;;
    *) KEYS_LATER=1 ;;
  esac
fi

step 6/6 "Пускам Genesis и отварям приложението"
genesis phone stop > /dev/null 2>&1 || true   # старата версия, ако тече
genesis phone pair
printf '\n\033[1;32mГотово.\033[0m Genesis тече на този телефон; чатът е в приложението Genesis.\n'
echo "Спиране: genesis phone stop   Пускане: genesis phone start   Обновяване: пусни този ред пак."
if [ -n "$KEYS_LATER" ]; then
  echo "Ключове: на компютъра пусни  genesis keys qr  → в приложението: меню ⋯ → Ключове от компютъра."
fi
