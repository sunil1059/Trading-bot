FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
WORKDIR /app
RUN useradd --create-home --uid 10001 bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./
COPY backend ./backend
COPY config ./config
COPY scripts ./scripts
USER bot
# Phase 2: apply migrations and validate config. The trading service is added in later phases.
CMD ["sh", "-c", "alembic upgrade head && python -m scripts.check_config"]
