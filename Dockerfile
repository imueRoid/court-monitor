FROM rust:1.100-slim-bookworm AS nas-build

WORKDIR /build
COPY nas/Cargo.toml nas/Cargo.lock ./
COPY nas/src ./src
RUN cargo build --locked --release

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Seoul OMP_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 court && useradd -u 10001 -g court -m court \
    && mkdir -p /data && chown court:court /data
WORKDIR /app
COPY --from=nas-build /build/target/release/court-monitor-nas /usr/local/bin/court-monitor-nas
COPY court_client.py case_summary.py captcha_ocr.py ./
COPY server ./server
COPY requirements-server.lock.txt ./
RUN pip install --no-cache-dir -r requirements-server.lock.txt
ARG APP_VERSION=development
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG APP_RUNTIME=development
ENV COURT_MONITOR_VERSION=${APP_VERSION} COURT_MONITOR_REVISION=${VCS_REF} COURT_MONITOR_CREATED=${BUILD_DATE} COURT_MONITOR_RUNTIME=${APP_RUNTIME}
LABEL org.opencontainers.image.title="Court Monitor" \
      org.opencontainers.image.source="https://github.com/imueRoid/court-monitor" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
ENTRYPOINT ["/usr/local/bin/court-monitor-nas"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
