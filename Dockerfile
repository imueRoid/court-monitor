FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Seoul OMP_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 court && useradd -u 10001 -g court -m court
WORKDIR /app
COPY requirements.txt requirements-server.txt requirements-server.lock.txt ./
RUN pip install --no-cache-dir -r requirements-server.lock.txt
COPY court_client.py case_summary.py captcha_ocr.py ./
COPY server ./server
RUN mkdir -p /data && chown -R court:court /data /app
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["python", "-m", "uvicorn", "server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
