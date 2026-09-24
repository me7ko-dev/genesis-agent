# Образът на задачите. Контекст: коренът на репото.
#   ./cloud/build.sh  (прави wheels и после docker build)
# Зависимостите идват като готови wheels (cloud/.wheels) — build-ът не тегли
# нищо от мрежата, затова е еднакъв на всеки сървър и не зависи от PyPI.
FROM mirror.gcr.io/library/python:3.12-slim

COPY cloud/.wheels /wheels
COPY cloud/runner/task-requirements.txt /tmp/task-requirements.txt
RUN pip install --no-cache-dir --no-index --find-links /wheels genesis-agent \
      -r /tmp/task-requirements.txt \
 && rm -rf /wheels \
 && useradd --create-home --uid 10001 agent

COPY cloud/__init__.py /opt/cloud/__init__.py
COPY cloud/runner /opt/cloud/runner
# Мярката за етап 1: същият бенчмарк като на лаптопа, но в затворения контейнер.
COPY scripts/bench_fix.py /opt/bench_fix.py
ENV PYTHONPATH=/opt PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python", "-m", "cloud.runner.task"]
