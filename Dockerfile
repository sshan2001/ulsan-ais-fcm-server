FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r portmis_collector/requirements_portmis_collector.txt

CMD ["bash", "-lc", "python portmis_collector/portmis_auto_collector_v0_1.py --headless --days 7 --server-url \"${PORTMIS_SERVER_URL:-https://ulsan-ais-fcm-server.onrender.com}\" --api-key \"${PORTMIS_API_KEY:-ulsan_ais_2026_mobile}\""]
