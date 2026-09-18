FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/wcl-bot.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Non-root. A named volume on /data picks up this ownership automatically.
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app:app /data
USER app
VOLUME /data

CMD ["python", "-m", "bot"]
