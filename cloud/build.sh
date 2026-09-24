#!/usr/bin/env bash
# Строи трите образа (задачи, прокси, шлюз). Пуска се от корена на репото: ./cloud/build.sh
# Wheels се свалят за Python-а и процесора на ОБРАЗА, не на машината, която
# строи (иначе cp311 wheels не стават за python:3.12). ARM сървър (Hetzner CAX):
#   PLATFORM=manylinux2014_aarch64 ./cloud/build.sh
set -euo pipefail
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"
rm -rf cloud/.wheels
python -m pip wheel --quiet --no-deps --wheel-dir cloud/.wheels .
python -m pip download --quiet --only-binary=:all: --dest cloud/.wheels \
  --python-version 3.12 --implementation cp --abi cp312 --platform "$PLATFORM" \
  cloud/.wheels/genesis_agent-*.whl -r cloud/runner/task-requirements.txt
docker build -f cloud/runner/Dockerfile -t genesis-runner:latest .
docker build -f cloud/egress/Dockerfile -t genesis-egress:latest .
docker build -f cloud/gateway/Dockerfile -t genesis-gateway:latest .
