"""FastAPI-приложение: подключает роутер `/ws`, отдаёт `/health`, статику фронтенда и сэмплы.

Веб-слой тонкий и расчётов не делает — вся математика в `core/`. Здесь только:

- подключение WebSocket-роутера `/ws` (реализация — `app/ws.py`, контракт — `.claude/rules/protocol.md`);
- `/health` для healthcheck контейнера;
- выдача сэмплов сценариев из `./samples`, чтобы жюри могло загрузить пример прямо из интерфейса,
  не разыскивая файлы в образе; это раздача статики с генерируемым индексом, а не расчётная ручка —
  протокол `/ws` остаётся единственным каналом для вычислений;
- раздача собранной статики Nuxt из `./static` и SPA-fallback на `200.html` для клиентских
  маршрутов (`nuxt generate`, `ssr:false`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.status import HTTP_404_NOT_FOUND

logger = logging.getLogger("app.main")

# Всё считаем от расположения этого файла, а не от текущей рабочей директории процесса —
# `uvicorn app.main:app` должен одинаково находить static/samples независимо от того, откуда его
# запустили. backend/app/main.py -> parent.parent = backend/ (в образе это WORKDIR /app).
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

STATIC_DIR = BACKEND_DIR / "static"  # Dockerfile: `nuxt generate` -> .output/public -> сюда
SPA_FALLBACK = STATIC_DIR / "200.html"  # SPA-fallback Nuxt для клиентских маршрутов фронтенда

# В образе Dockerfile копирует `Данные/` в `./samples` рядом с приложением. При локальном запуске
# без сборки образа (см. docs/README.md, раздел «без Docker») такой копии нет — используем сами
# `Данные/` в корне репозитория, чтобы сэмплы были доступны и разработчику без лишних шагов.
SAMPLES_DIR = BACKEND_DIR / "samples"
if not SAMPLES_DIR.is_dir() and (REPO_ROOT / "Данные").is_dir():
    SAMPLES_DIR = REPO_ROOT / "Данные"

app = FastAPI(title="Проектирование устойчивой спутниковой группировки")

# Сервис отдаётся жюри по одной ссылке на собранный образ (см. README), где фронтенд и бэкенд
# живут в одном origin — CORS там не нужен. Разрешаем любые источники ради локальной разработки,
# когда `nuxt dev` поднят на отдельном порту и ходит на этот бэкенд напрямую.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- /ws --------------------------------------------------------------------------------------
# Реализация протокола собирается параллельно в app/ws.py. Подключаем защищённо: /health и
# статика — независимые подсистемы одного процесса, поломка или временное отсутствие роутера
# не должна ронять весь сервис на старте (в т.ч. пока модуль ещё не готов параллельным агентом).
try:
    from app.ws import router as ws_router
except Exception:  # noqa: BLE001 — на старте нужен весь спектр ошибок импорта модуля, не только его отсутствие
    logger.exception("Роутер /ws не подключён (app/ws.py недоступен или падает при импорте)")
else:
    app.include_router(ws_router)
    logger.info("Роутер /ws подключён")


@app.get("/health")
def health() -> dict:
    """Живость контейнера. Ничего не считает — только отвечает."""
    return {"status": "ok"}


# --- сэмплы сценариев --------------------------------------------------------------------------
@app.get("/samples")
def list_samples() -> list[dict]:
    """Список сэмплов из `./samples`, без зашитых имён — жюри может подложить свои файлы того же
    формата, и они появятся здесь сами."""
    if not SAMPLES_DIR.is_dir():
        return []
    files = sorted(p for p in SAMPLES_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    return [{"name": p.name, "size_bytes": p.stat().st_size, "url": f"/samples/{p.name}"} for p in files]


@app.get("/samples/{name}")
def get_sample(name: str) -> FileResponse:
    """Отдаёт файл сэмпла по имени из списка `/samples`.

    `{name}` — один сегмент пути (FastAPI не пускает в него `/`), поэтому подъём выше `SAMPLES_DIR`
    через `..` синтаксически невозможен; `relative_to` ниже — вторая, явная проверка того же факта.
    """
    candidate = (SAMPLES_DIR / name).resolve()
    try:
        candidate.relative_to(SAMPLES_DIR.resolve())
    except ValueError:
        raise HTTPException(HTTP_404_NOT_FOUND, "Файл не найден") from None
    if not candidate.is_file() or candidate.suffix.lower() != ".json":
        raise HTTPException(HTTP_404_NOT_FOUND, "Файл не найден")
    return FileResponse(candidate, media_type="application/json")


# --- статика фронтенда + SPA-fallback ------------------------------------------------------------
# Регистрируется последним: более ранние маршруты (/health, /samples, /ws) всегда в приоритете,
# а на любой прочий путь отдаём файл сборки Nuxt или `200.html`, чтобы клиентский роутер сам
# разобрался, какой экран показать (обычный SPA-fallback для истории без серверного рендера).
@app.get("/{full_path:path}")
def spa(full_path: str) -> FileResponse:
    if not STATIC_DIR.is_dir():
        raise HTTPException(
            HTTP_404_NOT_FOUND,
            "Статика фронтенда не собрана: соберите frontend/ (`nuxt generate`) в backend/static/",
        )
    candidate = (STATIC_DIR / full_path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
        is_inside = True
    except ValueError:
        is_inside = False
    if is_inside and candidate.is_file():
        return FileResponse(candidate)
    if SPA_FALLBACK.is_file():
        return FileResponse(SPA_FALLBACK)
    raise HTTPException(HTTP_404_NOT_FOUND, "SPA-fallback 200.html не найден в static/")
