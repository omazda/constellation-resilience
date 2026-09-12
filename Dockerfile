# Один образ, две стадии: node собирает статику фронтенда, python отдаёт её вместе с расчётным
# ядром через FastAPI. Шаблон — .claude/rules/docs.md. Тесты (`pytest`) в образ не входят —
# гоняются отдельно командой из README, чтобы не тащить pytest в продакшен-слой.

# ── стадия 1: фронтенд (Nuxt 4, SPA, ssr:false) ──────────────────────────────────────────────
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run generate
# nuxt generate -> .output/public — статическая сборка SPA, серверный рендер не нужен
# (frontend/nuxt.config.ts: ssr:false, .claude/rules/frontend.md).

# ── стадия 2: бэкенд (FastAPI + core/numpy) + собранная статика ──────────────────────────────
FROM python:3.12-slim AS app
WORKDIR /app

# Зависимости — отдельным слоем от кода, чтобы правки backend/ не инвалидировали кеш pip install.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
# backend/core/geometry.py — побайтовая копия эталона «Расчетный модуль/geometry.py»
# (backend/core/__init__.py); сам эталонный каталог в образ не копируется — он нужен только
# tools/golden.py при разработке, а не рантайму сервиса.

COPY --from=web /web/.output/public ./static
# app/main.py ищет собранный фронтенд в ./static относительно расположения самого себя
# (BACKEND_DIR = backend/app/../.. = /app) и отдаёт SPA-fallback на static/200.html.

COPY Данные/ ./samples/
# Сэмплы сценариев — жюри открывает их прямо из интерфейса (`UFileUpload`/список `/samples`,
# backend/app/main.py), не разыскивая файлы в репозитории.

# Процесс приложения не должен работать от root без необходимости.
RUN useradd --system --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
