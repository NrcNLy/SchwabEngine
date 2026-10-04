# ---- Stage 1: build the dashboard (tsc + vite build -> /web/dist) --------------------------
FROM node:20-slim AS web
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json tsconfig.node.json vite.config.ts postcss.config.js tailwind.config.js ./
COPY src ./src
RUN npm run build

# ---- Stage 2: Python engine + API, serving the built dashboard ------------------------------
FROM python:3.12-slim

# Prevent Python from writing pyc files and keep stdout unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=America/New_York

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (main.py, governor.py, core/, services/, execution/, api/, config/, ...)
COPY . .
COPY --from=web /web/dist ./dist

EXPOSE 8080

# Default is DRY-RUN. Live trading is opt-in: pass `--live` (see remote_deploy.sh ENGINE_FLAGS).
ENTRYPOINT ["python", "main.py"]
CMD []
