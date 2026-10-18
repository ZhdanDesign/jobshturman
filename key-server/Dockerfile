FROM python:3.12-slim
WORKDIR /app
# Build context is the repository root (compose.yaml): the server shares the methodology route with hrs.
COPY core /core
COPY key-server/pyproject.toml ./
COPY key-server/hrs_keys ./hrs_keys
RUN pip install --no-cache-dir /core . && useradd --system --uid 1001 keys && mkdir /data && chown keys /data
USER keys
ENV HRS_KEYS_DATA=/data
EXPOSE 8098
CMD ["hrs-keys", "serve", "--host", "0.0.0.0", "--port", "8098"]
