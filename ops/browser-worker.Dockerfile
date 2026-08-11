FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

RUN python3 -m pip install --no-cache-dir playwright==1.55.0

WORKDIR /app
COPY ops/browser_worker.py /app/ops/browser_worker.py
RUN mkdir -p /data/browser_profiles && chown -R pwuser:pwuser /data
USER pwuser

CMD ["python3", "/app/ops/browser_worker.py", "--host", "0.0.0.0", "--port", "8765", "--profile-root", "/data/browser_profiles"]
