# Official scorer Python/SQLite environment; only public pinned baseline download.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY rwx-blast-radius/requirements-runtime.txt /app/requirements-runtime.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements-runtime.txt
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sqlglot && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /opt/sqlglot && rm /tmp/sqlglot.tar.gz
COPY rwx-blast-radius/selector.py rwx-blast-radius/scheduler.py rwx-blast-radius/patches.py rwx-blast-radius/class_selection.py rwx-blast-radius/import_guard.py rwx-blast-radius/runtime_probe.py rwx-blast-radius/factory_selection.py rwx-blast-radius/factory_proof_format.py /app/
COPY rwx-blast-radius/maps/coverage.json.gz rwx-blast-radius/maps/factory_proofs.json /app/maps/
# Experimental evidence is packaged but never enabled by the default command.
COPY rwx-blast-radius/comparison_selection.py rwx-blast-radius/comparison_proof.py rwx-blast-radius/comparison_edit.py rwx-blast-radius/comparison_audit.py /app/
COPY rwx-blast-radius/maps/comparison-experimental.json.gz rwx-blast-radius/maps/comparison-experimental-manifest.json /app/maps/
CMD ["python", "/app/selector.py", "--class-guard", "--factory-proofs", "/app/maps/factory_proofs.json", "--metrics"]
