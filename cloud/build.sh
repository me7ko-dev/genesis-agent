#!/usr/bin/env bash
# Строи трите образа (задачи, прокси, шлюз): ./cloud/build.sh — от която и да е папка.
# Wheels се свалят за Python-а и процесора на ОБРАЗА, не на машината, която
# строи (иначе cp311 wheels не стават за python:3.12). ARM сървър (Hetzner CAX):
#   PLATFORM=manylinux2014_aarch64 ./cloud/build.sh
# Всеки образ е с два етикета: :latest и :<git sha> — за връщане назад:
#   docker tag genesis-runner:<стар sha> genesis-runner:latest
set -euo pipefail
# Деплой одит 2026-10-11: от друга папка падаше с „Directory '.' is not
# installable“; на Ubuntu Server има python3, но не и `python`.
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
DOCKER="${DOCKER:-docker}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
rm -rf cloud/.wheels
"$PYTHON" -m pip wheel --quiet --no-deps --wheel-dir cloud/.wheels .
"$PYTHON" -m pip download --quiet --only-binary=:all: --dest cloud/.wheels \
  --python-version 3.12 --implementation cp --abi cp312 --platform "$PLATFORM" \
  cloud/.wheels/genesis_agent-*.whl -r cloud/runner/task-requirements.txt
for image in runner egress gateway; do
  "$DOCKER" build -f "cloud/$image/Dockerfile" -t "genesis-$image:latest" -t "genesis-$image:$SHA" .
done
echo "готово: genesis-{runner,egress,gateway}:latest = :$SHA"
