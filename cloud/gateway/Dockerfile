# Шлюзът за моделите. Само стандартната библиотека — няма какво да се инсталира.
FROM mirror.gcr.io/library/python:3.12-slim
RUN useradd --uid 10003 gateway
COPY cloud/__init__.py /opt/cloud/__init__.py
COPY cloud/gateway /opt/cloud/gateway
ENV PYTHONPATH=/opt PYTHONDONTWRITEBYTECODE=1
USER 10003:10003
EXPOSE 8090
ENTRYPOINT ["python", "-m", "cloud.gateway.gateway"]
