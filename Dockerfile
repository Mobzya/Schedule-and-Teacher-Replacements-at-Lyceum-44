FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN addgroup --system bot && adduser --system --ingroup bot bot \
    && mkdir -p /var/data \
    && chown -R bot:bot /app /var/data
ENV DATABASE_PATH=/var/data/schedule_v3.db
USER bot
CMD ["python", "-m", "app.main"]
