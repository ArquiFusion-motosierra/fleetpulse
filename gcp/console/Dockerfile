# Cloud Run `console` (T15): React compilado + FastAPI (BFF) en una sola imagen.
FROM node:20.18.1-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12.8-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 STATIC_DIR=/srv/web/dist
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY --from=web /web/dist web/dist
RUN useradd --uid 10001 --no-create-home console
USER console
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --no-access-log
