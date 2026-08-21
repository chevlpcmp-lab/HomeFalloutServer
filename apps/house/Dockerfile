# Overseer. One stage: there is nothing to compile, and the slim image plus five
# pinned wheels is the whole footprint.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# /data is the PVC mount; the Deployment's fsGroup matches this uid so SQLite can
# write its WAL files there.
RUN useradd --system --uid 10001 --no-create-home overseer
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
