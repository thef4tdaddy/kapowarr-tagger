FROM python:3.12-slim
WORKDIR /app
COPY companion.py /app/companion.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1029:100
CMD ["python", "companion.py"]
