FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    FLEET_AGENT_MODE=structured

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "playwright==1.60.0"

COPY playwright_automation/ playwright_automation/
COPY scripts/ scripts/

RUN mkdir -p profiles accounts logs

ENV ACCOUNT_ID="" \
    PROXY_URL="" \
    FLEET_MODE=1 \
    FLEET_STAGGER_MIN=15 \
    FLEET_STAGGER_MAX=90 \
    FLEET_OLLAMA_MIN_INTERVAL_SEC=8.0

ENTRYPOINT ["python", "scripts/docker_entrypoint.py"]
