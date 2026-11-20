# Official scorer Python/SQLite environment; only public pinned baseline download.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sqlglot && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /opt/sqlglot && rm /tmp/sqlglot.tar.gz
COPY rwx-blast-radius/selector.py rwx-blast-radius/scheduler.py rwx-blast-radius/patches.py /app/
CMD ["python", "/app/selector.py"]
