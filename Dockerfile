FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TG_CONFIG=/app/data/bots.json \
    TG_SESSION=/app/data/tg_checkin.session \
    TG_SETTINGS=/app/data/settings.json \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tg_checkin.py web_server.py ./
COPY web ./web
COPY bots.json /app/bots.json
COPY mihomo/config.yaml /app/mihomo/config.yaml
COPY entrypoint.sh /app/entrypoint.sh
RUN mkdir -p /app/data
RUN chmod 755 /app/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn web_server:app --host ${WEB_HOST} --port ${WEB_PORT}"]
