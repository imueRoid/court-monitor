FROM rust:1.100.0-slim-bookworm AS nas-build

RUN --mount=type=bind,source=nas,target=/source \
    --mount=type=tmpfs,target=/build-output \
    cd /source \
    && export CARGO_HOME=/build-output/cargo CARGO_TARGET_DIR=/build-output/target \
    && rustc --version \
    && cargo build --locked --release \
    && cp "$CARGO_TARGET_DIR/release/court-monitor-nas" /court-monitor-nas

FROM python:3.12-slim-bookworm AS ocr-assets

COPY nas/ocr-assets.requirements nas/extract_ocr_assets.py /build/
RUN pip download --no-cache-dir --only-binary=:all: --no-deps --require-hashes \
        -r /build/ocr-assets.requirements -d /build/wheels \
    && python /build/extract_ocr_assets.py /build/wheels /opt/court-ocr \
    && rm -rf /build/wheels

FROM debian:bookworm-slim

ENV TZ=Asia/Seoul
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 court && useradd -u 10001 -g court -m court \
    && mkdir -p /data && chown court:court /data
WORKDIR /app
COPY --from=nas-build /court-monitor-nas /usr/local/bin/court-monitor-nas
RUN ln -s court-monitor-nas /usr/local/bin/python
COPY --from=ocr-assets --chmod=u=rwX,go=rX /opt/court-ocr /opt/court-ocr
COPY --chmod=u=rwX,go=rX server/static ./server/static
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
    CMD ["/usr/local/bin/court-monitor-nas", "--healthcheck", "8080"]
ENTRYPOINT ["/usr/local/bin/court-monitor-nas"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
