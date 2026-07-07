# ACBC GivEnergy Dashboard — container image.
#
# Multi-arch: linux/amd64 (PCs / NAS) and linux/arm64 (Raspberry Pi 4/5 on a
# 64-bit OS). The dashboard talks to the inverter dongle OUTBOUND over TCP, so no
# device passthrough is needed — just LAN reachability and the published web port.
#
# Listen mode (the default for all supported inverters) needs no extra libraries,
# so the image ships only Flask + waitress. The optional `givenergy-modbus` poll
# library is intentionally not bundled; see README for the advanced poll-mode note.
FROM python:3.11-slim

# TZ matters: the scheduler and BST tariff logic use local time. The default UTC
# would put the scheduler an hour out in summer — set e.g. -e TZ=Europe/London.
ENV TZ=Etc/UTC \
    ACBC_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl gosu \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "flask>=3.0" "waitress>=3.0"

WORKDIR /app

# Code + static assets only. Writable state lives on the /data volume (ACBC_DATA_DIR).
COPY dashboard_server.py dashboard.html manifest.json sw.js VERSION ./
COPY icons ./icons
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Unprivileged app user; the entrypoint hands the data volume to it at startup.
RUN useradd --uid 10001 --home-dir /app --shell /usr/sbin/nologin --no-create-home acbc \
 && chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 7890

# Liveness probe: /healthz is 200 whenever the web app is up. (Do NOT use
# /api/data — it returns 503 when the inverter is unreachable, which would flag
# a perfectly healthy container as unhealthy on any inverter blip.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://localhost:7890/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "dashboard_server.py"]
