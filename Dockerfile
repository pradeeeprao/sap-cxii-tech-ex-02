# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EMBEDDING_CACHE_DIR=/models

RUN python -m venv "$VIRTUAL_ENV"
COPY requirements.txt /build/requirements.txt
RUN pip install --upgrade pip && pip install -r /build/requirements.txt

WORKDIR /build
COPY app.py etl.py llm.py semantic_index.py settings.py sql_query.py ./
COPY data/orders.csv data/orders.csv

# Download model weights and materialize a seed database/index at build time.
# Runtime pods therefore do not need internet access to become ready.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', cache_folder='/models')" && \
    python etl.py load data/orders.csv \
      --db /build/seed/orders.db \
      --index /build/seed/orders-index.npz


FROM python:3.12-slim AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/orders.db \
    SEMANTIC_INDEX_PATH=/app/data/orders-index.npz \
    EMBEDDING_CACHE_DIR=/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update && \
    apt-get install --no-install-recommends -y libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app && \
    mkdir -p /app/data /app/seed && \
    chown -R app:app /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=app:app /models /models
COPY --from=builder --chown=app:app /build/seed /app/seed
COPY --chown=app:app app.py etl.py llm.py semantic_index.py settings.py sql_query.py /app/
RUN cp /app/seed/orders.db /app/data/orders.db && \
    cp /app/seed/orders-index.npz /app/data/orders-index.npz && \
    chown -R app:app /app/data

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
