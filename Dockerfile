# The mock as a container: handler/server.py serves handler.py over HTTP. Stdlib only, no pip install.
# docker build -t aigw-mock-llm . && docker run --rm -e TOKEN=<secret> -p 8080:8080 aigw-mock-llm
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
WORKDIR /app
COPY handler/handler.py handler/server.py ./
RUN chmod 644 handler.py server.py
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/healthz\", timeout=2)"]
CMD ["python3", "server.py"]
