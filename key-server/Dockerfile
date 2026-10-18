FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY hrs_keys ./hrs_keys
RUN pip install --no-cache-dir . && useradd --system --uid 1001 keys && mkdir /data && chown keys /data
USER keys
ENV HRS_KEYS_DATA=/data
EXPOSE 8098
CMD ["hrs-keys", "serve", "--host", "0.0.0.0", "--port", "8098"]
