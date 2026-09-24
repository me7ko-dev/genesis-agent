# Изходящото прокси. Само стандартната библиотека — няма какво да се инсталира.
FROM mirror.gcr.io/library/python:3.12-slim
RUN useradd --uid 10002 egress
COPY cloud/__init__.py /opt/cloud/__init__.py
COPY cloud/egress /opt/cloud/egress
ENV PYTHONPATH=/opt PYTHONDONTWRITEBYTECODE=1
USER 10002:10002
EXPOSE 3128
ENTRYPOINT ["python", "-m", "cloud.egress.proxy"]
