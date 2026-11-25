# --- build the React app ---
FROM node:22-alpine AS web
WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json web/
RUN npm ci
COPY web web
RUN npm run build

# --- Python backend (serves the API, the Telegram webhook and the built web app) ---
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 WEB_DIST=/app/web/dist
WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/tripwise backend/tripwise
RUN pip install ./backend && useradd --system app
COPY --from=web /app/web/dist web/dist
USER app
EXPOSE 3000
CMD ["python", "-m", "tripwise"]
