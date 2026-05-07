FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Hong_Kong \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DISPLAY=:99 \
    ENABLE_NOVNC=1 \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    SCREEN_GEOMETRY=1280x800x24 \
    AETHERSWAP_DOCKER=1 \
    AETHERSWAP_NOVNC_URL=auto

WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fluxbox \
        fonts-noto-cjk \
        novnc \
        python3.12-venv \
        tini \
        tzdata \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && printf 'playwright==1.55.0\n' > /tmp/docker-constraints.txt \
    && pip install --no-cache-dir -c /tmp/docker-constraints.txt -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/config /app/log

EXPOSE 28472 6080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:28472/api/status', timeout=5)"

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "28472", "--log-level", "warning"]
