FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir libarchive-c==5.3
WORKDIR /app
COPY companion.py writer.py service.py cbr.py discord_notify.py retention.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1029:100
HEALTHCHECK --interval=5m --timeout=10s --start-period=30m --retries=3 CMD python -c "import json,time,os; from pathlib import Path; p=Path(os.environ.get('STATE_DIR','/state'))/'health.json'; assert time.time()-json.loads(p.read_text())['last_success'] < max(1800,3*int(os.environ.get('POLL_SECONDS','300')))"
CMD ["python", "service.py"]
