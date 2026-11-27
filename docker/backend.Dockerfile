# Stage 1: build the static dashboard
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

# Stage 2: API + trading engine (PAPER / SIGNAL_ONLY only; LIVE is locked in this build)
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
COPY --from=web /web/out ./frontend/out
RUN mkdir -p /app/data && chown bot /app/data
USER bot
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && python -m scripts.check_config && python -m scripts.run_server"]
