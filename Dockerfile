FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data
WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --user-group --create-home itinerario \
    && mkdir -p /app/data \
    && chown -R itinerario:itinerario /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY itinerario ./itinerario

USER itinerario:itinerario
CMD ["python", "-m", "itinerario"]
