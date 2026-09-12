"""Веб-слой: единственный эндпоинт `/ws`, конверт `{id, type, payload}`, семь типов сообщений.

Слой тонкий: разбирает конверт (`protocol.py`), проверяет форму `payload` конкретного типа,
зовёт `core/` за расчётом и отдаёт результат тем же `id`. Ни одной формулы или алгоритма
маршрутизации здесь нет — вся математика в `core/`, этот модуль её только оркеструет и
раскладывает в проводной формат. Полный контракт (все семь типов, коды ошибок, разбор
бинарного фрейма) — докстринг `protocol.py`; здесь — только реализация.

Два требования, которые этот модуль обязан соблюдать (легко нарушить незаметно):

  1. Расчёт горизонта (`core.metrics.compute` + сбор геометрии по 720 отсчётам) идёт ТОЛЬКО
     через `await asyncio.to_thread(...)` (см. `_run_with_progress`) — синхронный numpy внутри
     `async def`-обработчика заблокировал бы event loop для ВСЕХ подключений разом, `progress`
     не ушёл бы, а клиент решил бы, что соединение зависло.
  2. Результат `compute`/`attach` уходит ОДНИМ пакетом на весь горизонт: JSON-манифест (текстовый
     фрейм) сразу за ним — один бинарный фрейм со всеми позициями/рёбрами/маршрутами сразу по
     всем 720 отсчётам (см. `_compute_pipeline`, формат — `protocol.pack_sections`). Раздача
     по одному отсчёту убила бы обе цели: скраб шкалы без запросов и щадящий трафик (~0.6–1 МБ
     на вариант вместо тысяч мелких сообщений).

Результат кешируется в памяти процесса по ключу `(variant_id, strategy)`, где `variant_id` —
это `core.scenario.scenario_hash(scenario)` (см. докстринг `_Store`): `attach` после обрыва
соединения отдаёт готовый кеш без пересчёта, как и требует контракт, а не запускает его заново.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import delivery, metrics, network, routing
from core import scenario as scenario_mod

from . import protocol

__all__ = ["router", "reset_store"]

_LOG = logging.getLogger(__name__)

router = APIRouter()

_STRATEGIES: tuple[str, ...] = ("hops", "distance")
_REASON_INDEX: dict[str, int] = {name: i for i, name in enumerate(routing.REASONS)}


# ─────────────────────────────── хранилище вариантов и кеша ────────────────────────────────

@dataclass
class ComputeResult:
    """Готовый пакет `compute`/`attach` плюс необработанный результат `core.metrics.compute()`
    (нужен `snapshot`/`export`/`compare` без повторного расчёта — только чтение уже посчитанного)."""

    manifest: dict[str, Any]
    binary: bytes
    computed: dict[str, Any]
    routes_index: dict[tuple[int, str], list[str]]
    created_at: float


class _Store:
    """Внутрипроцессное хранилище вариантов сценария и кеша расчёта.

    Сознательно простой словарь в памяти процесса, а не файл/БД: этого достаточно для
    «сохранить вариант и вернуться к нему» (DoD п.1) в рамках одной работающей сессии сервиса
    и для `attach` после обрыва СОЕДИНЕНИЯ (не после перезапуска процесса — переживание рестарта
    сервиса контрактом не требуется, только повторный расчёт после разрыва сокета).

    Ключевое свойство: `variant_id = core.scenario.scenario_hash(scenario)` — детерминирован по
    содержимому. Значит: (а) повторная загрузка/патч, давшие тот же по смыслу сценарий, возвращают
    тот же `variant_id` без дублирования записи; (б) кеш результата по `scenario_hash`, который
    требует контракт, — это и есть кеш по `variant_id`, отдельного ключа не нужно.
    """

    def __init__(self) -> None:
        self.variants: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.results: dict[tuple[str, str], ComputeResult] = {}
        self.pending: dict[tuple[str, str], asyncio.Future] = {}
        self._load_from_disk()

    # ── хранение на диске ───────────────────────────────────────────────────────────────
    # Варианты переживают перезапуск сервиса: «сохранить вариант и вернуться к нему» теряет
    # смысл, если возврат работает только до конца жизни процесса. Формат — тот же
    # cosmo-A-1.0, что и на входе, поэтому файл варианта можно просто открыть или отдать
    # обратно в сервис. Кеш расчёта на диск НЕ кладём: он выводится из сценария за секунду,
    # а весит сотни килобайт.

    @property
    def dir(self) -> Path:
        d = Path(os.environ.get("CONSTELLATION_DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "variants"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_from_disk(self) -> None:
        try:
            files = sorted(self.dir.glob("*.json"))
        except OSError:
            return
        for f in files:
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
                s = scenario_mod.load_scenario(doc["scenario"])
            except Exception:
                # Битый или устаревший файл не должен мешать запуску сервиса.
                continue
            variant_id = f.stem
            self.variants[variant_id] = s
            self.meta[variant_id] = doc.get("meta", {})

    def save(self, variant_id: str, s: dict[str, Any], meta: dict[str, Any]) -> None:
        self.variants[variant_id] = s
        self.meta[variant_id] = meta
        try:
            (self.dir / f"{variant_id}.json").write_text(
                json.dumps({"meta": meta, "scenario": scenario_mod.export_scenario(s)},
                           ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            # Диск недоступен (read-only контейнер) — работаем как раньше, из памяти.
            pass

    def listing(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {"variant_id": vid, **self.meta.get(vid, {}), "summary": _summary(s)}
                for vid, s in self.variants.items()
            ),
            key=lambda r: (r.get("created") or ""),
        )


_STORE = _Store()

# Анализ устойчивости считается секунды и не меняется для уже зафиксированного варианта
# (variant_id — хеш сценария), поэтому достаточно словаря без вытеснения: вариантов за сессию
# единицы, а не тысячи.
_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}


def reset_store() -> None:
    """Только для тестов: обнуляет глобальное состояние между независимыми кейсами."""
    _STORE.variants.clear()
    _STORE.results.clear()
    _STORE.pending.clear()


# ───────────────────────── прогресс синхронного расчёта в потоке ───────────────────────────

async def _run_with_progress(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    on_progress: Callable[[int, str], Awaitable[None]],
) -> Any:
    """Запускает синхронную `func(*args, progress_cb)` через `asyncio.to_thread` (требование 1
    докстринга модуля). `func` зовёт `progress_cb(pct, stage)` из ПОТОКА — трогать asyncio оттуда
    напрямую нельзя, поэтому колбэк только кладёт сообщение в очередь через
    `loop.call_soon_threadsafe`, а отдельная корутина-потребитель на event loop разбирает очередь
    и уже безопасно зовёт `await on_progress(...)` (это она шлёт `progress`-конверт в сокет)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()

    def progress_cb(pct: int, stage: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (pct, stage))

    async def _consume() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            pct, stage = item
            await on_progress(pct, stage)

    consumer = asyncio.create_task(_consume())
    try:
        return await asyncio.to_thread(func, *args, progress_cb)
    finally:
        await queue.put(None)
        await consumer


# ────────────────────────────── синхронный расчётный конвейер ──────────────────────────────

def _compute_pipeline(
    s: dict[str, Any],
    variant_id: str,
    strategy: str,
    progress_cb: Callable[[int, str], None],
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Строит манифест + бинарный буфер на весь горизонт. Выполняется в отдельном потоке
    (см. `_run_with_progress`) — ничего асинхронного внутри быть не должно.

    Источник истины для доступности/видимости/маршрутов — ЕДИНСТВЕННЫЙ вызов
    `core.metrics.compute()` (та же функция, что сверяется с `tools/golden.py`, см.
    `backend/tests/test_golden.py`): здесь её результат только раскладывается в типизированные
    массивы, никакая часть алгоритма достижимости/маршрутизации не переписывается заново.
    Дополнительный проход по `core.network.snapshot()` нужен ТОЛЬКО за тем, чего `compute()`
    не отдаёт (компакт по построению — доли/перерывы/маршруты, не сырые координаты и рёбра):
    позициями спутников и рёбрами сети для отрисовки, плюс причиной отсутствия пути там, где
    маршрута нет (`core.routing.no_route_reason`, тоже не переизобретается).
    """
    steps = scenario_mod.time_grid(s)
    n_steps = len(steps)
    step_s = int(s["environment"]["step_s"])

    progress_cb(0, "metrics")
    computed = metrics.compute(s, strategy=strategy)
    progress_cb(45, "metrics_done")

    sat_ids_set, ground_ids_set = network.split_node_kinds(s)
    sat_order = sorted(sat_ids_set)
    ground_order = sorted(ground_ids_set)
    node_ids = sat_order + ground_order
    assert len(node_ids) <= 0xFFFF, (
        f"{len(node_ids)} узлов — больше, чем помещается в uint16-индекс бинарного фрейма"
    )
    node_index = {nid: i for i, nid in enumerate(node_ids)}
    n_sat = len(sat_order)

    clients = sorted(g["id"] for g in s["ground_sites"] if g["role"] == "client")
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    n_clients = len(clients)

    # Ряды по клиенту уже посчитаны compute() — здесь только переупаковка в матрицу [T, C],
    # без повторного обхода снимков сети.
    hops = np.full((n_steps, n_clients), -1, dtype=np.int16)
    visible = np.zeros((n_steps, n_clients), dtype=np.uint8)
    for ci, client in enumerate(clients):
        rec = computed["clients"][client]
        hops[:, ci] = [h if h is not None else -1 for h in rec["hops"]]
        visible[:, ci] = [1 if v else 0 for v in rec["visible"]]

    # Сами маршруты (списки узлов) есть только в плоском routes — нужны для route_nodes ниже.
    routes_by_key: dict[tuple[int, str], list[str]] = {
        (r["t_s"], r["client_id"]): r["path"] for r in computed["routes"]
    }

    sat_lla = np.zeros((n_steps, n_sat, 3), dtype=np.float32)
    sat_active = np.zeros((n_steps, n_sat), dtype=np.uint8)
    edge_offsets = np.zeros(n_steps + 1, dtype=np.uint32)
    edge_pairs_list: list[tuple[int, int]] = []
    reason_codes = np.full((n_steps, n_clients), -1, dtype=np.int8)
    route_offsets = np.zeros(n_steps * n_clients + 1, dtype=np.uint32)
    route_nodes_list: list[int] = []

    report_every = max(1, n_steps // 20)
    for step_i, t in enumerate(steps):
        snap = network.snapshot(s, t)
        for sat in snap["satellites"]:
            si = node_index[sat["id"]]
            sat_lla[step_i, si, 0] = sat["lat_deg"]
            sat_lla[step_i, si, 1] = sat["lon_deg"]
            sat_lla[step_i, si, 2] = sat["alt_ratio"]
            sat_active[step_i, si] = 1 if sat["active"] else 0

        for e in snap["isl_edges"]:
            edge_pairs_list.append((node_index[e["a"]], node_index[e["b"]]))
        for gc in snap["ground_contacts"]:
            edge_pairs_list.append((node_index[gc["ground_id"]], node_index[gc["satellite_id"]]))
        edge_offsets[step_i + 1] = len(edge_pairs_list)

        adj: dict[str, set[str]] | None = None
        for ci, client in enumerate(clients):
            flat_idx = step_i * n_clients + ci
            path = routes_by_key.get((t, client)) or []
            if path:
                route_nodes_list.extend(node_index[nid] for nid in path)
            else:
                if adj is None:
                    adj = network.build_adjacency(snap)
                reason = routing.no_route_reason(s, t, adj, client, gateways, sat_ids_set)
                reason_codes[step_i, ci] = _REASON_INDEX[reason]
            route_offsets[flat_idx + 1] = len(route_nodes_list)

        if step_i % report_every == 0 or step_i == n_steps - 1:
            progress_cb(min(45 + round(55 * (step_i + 1) / n_steps), 99), "geometry")

    progress_cb(100, "done")

    edge_pairs = (
        np.array(edge_pairs_list, dtype=np.uint16) if edge_pairs_list else np.zeros((0, 2), dtype=np.uint16)
    )
    route_nodes = (
        np.array(route_nodes_list, dtype=np.uint16) if route_nodes_list else np.zeros((0,), dtype=np.uint16)
    )

    binary, layout = protocol.pack_sections([
        ("sat_lla", sat_lla),
        ("sat_active", sat_active),
        ("edge_offsets", edge_offsets),
        ("edge_pairs", edge_pairs),
        ("hops", hops),
        ("visible", visible),
        ("reason_codes", reason_codes),
        ("route_offsets", route_offsets),
        ("route_nodes", route_nodes),
    ])

    client_metrics = {
        c: {
            "vis_pct": computed["clients"][c]["vis_pct"],
            "avail_pct": computed["clients"][c]["avail_pct"],
            "max_gap_s": computed["clients"][c]["max_gap_s"],
            "gap_start_censored_s": computed["clients"][c]["gap_start_censored_s"],
            "gap_end_censored_s": computed["clients"][c]["gap_end_censored_s"],
        }
        for c in clients
    }

    manifest = {
        "variant_id": variant_id,
        "scenario_hash": variant_id,
        "strategy": strategy,
        "step_s": step_s,
        "n_steps": n_steps,
        "t_s": steps,
        "node_ids": node_ids,
        "n_sat": n_sat,
        "n_ground": len(ground_order),
        "clients": clients,
        "gateways": sorted(gateways),
        "client_metrics": client_metrics,
        "reason_labels": list(routing.REASONS),
        "binary": True,
        "layout": layout,
    }
    return manifest, binary, computed


async def _ensure_computed(
    variant_id: str,
    strategy: str,
    on_progress: Callable[[int, str], Awaitable[None]] | None = None,
) -> ComputeResult:
    """Отдаёт кеш `(variant_id, strategy)`, считает его через `_run_with_progress`, если кеша нет,
    или дожидается уже идущего расчёта того же ключа (несколько соединений одновременно попросили
    один и тот же вариант — считаем один раз, а не по разу на каждое)."""
    key = (variant_id, strategy)

    cached = _STORE.results.get(key)
    if cached is not None:
        if on_progress is not None:
            await on_progress(100, "cached")
        return cached

    pending = _STORE.pending.get(key)
    if pending is not None:
        if on_progress is not None:
            await on_progress(50, "waiting_shared")
        result = await pending
        if on_progress is not None:
            await on_progress(100, "cached")
        return result

    loop = asyncio.get_running_loop()
    future: asyncio.Future[ComputeResult] = loop.create_future()
    future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    _STORE.pending[key] = future
    try:
        s = _STORE.variants[variant_id]

        async def _progress(pct: int, stage: str) -> None:
            if on_progress is not None:
                await on_progress(pct, stage)

        manifest, binary, computed = await _run_with_progress(
            _compute_pipeline, (s, variant_id, strategy), _progress,
        )
        routes_index = {(r["t_s"], r["client_id"]): r["path"] for r in computed["routes"]}
        result = ComputeResult(
            manifest=manifest, binary=binary, computed=computed,
            routes_index=routes_index, created_at=time.time(),
        )
        _STORE.results[key] = result
        future.set_result(result)
        return result
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        _STORE.pending.pop(key, None)


# ─────────────────────────────────── разбор payload ────────────────────────────────────────

def _require_str(payload: dict[str, Any], key: str) -> str:
    val = payload.get(key)
    if not isinstance(val, str) or val == "":
        raise protocol.ProtocolError("bad_request", f"payload.{key}", f"поле {key} должно быть непустой строкой")
    return val


def _require_strategy(payload: dict[str, Any]) -> str:
    val = payload.get("strategy", "hops")
    if val not in _STRATEGIES:
        raise protocol.ProtocolError(
            "bad_request", "payload.strategy",
            f"strategy должна быть одной из {_STRATEGIES}, получено {val!r}",
        )
    return val


def _require_variant(payload: dict[str, Any], key: str = "variant_id") -> tuple[str, dict[str, Any]]:
    variant_id = _require_str(payload, key)
    s = _STORE.variants.get(variant_id)
    if s is None:
        raise protocol.ProtocolError(
            "not_found", f"payload.{key}",
            f"вариант {variant_id!r} не найден — сначала пришлите scenario.load или scenario.patch",
        )
    return variant_id, s


def _require_t_s(payload: dict[str, Any], s: dict[str, Any]) -> int:
    val = payload.get("t_s")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise protocol.ProtocolError("bad_request", "payload.t_s", "поле t_s должно быть числом секунд")
    t_s = int(val)
    step_s = int(s["environment"]["step_s"])
    horizon_s = int(s["environment"]["horizon_s"])
    if t_s != val or not (0 <= t_s < horizon_s) or t_s % step_s != 0:
        raise protocol.ProtocolError(
            "bad_request", "payload.t_s",
            f"t_s должен быть узлом сетки: 0, {step_s}, ..., {horizon_s - step_s} (шаг {step_s})",
        )
    return t_s


def _summary(s: dict[str, Any]) -> dict[str, Any]:
    """Сводка сценария для мгновенного отображения в UI без ожидания `compute` — ни одного
    жёстко зашитого ID/имени пункта, только форма и границы взятые из самого сценария."""
    e, d = s["environment"], s["design"]
    ground = s["ground_sites"]
    clients = [g for g in ground if g["role"] == "client"]
    gateways = [g for g in ground if g["role"] == "gateway"]
    return {
        "meta": s.get("meta"),
        "n_planes": len(d["planes"]),
        "n_satellites": len(d["satellites"]),
        "n_ground_sites": len(ground),
        "n_clients": len(clients),
        "n_gateways": len(gateways),
        "launch_stage": d["launch_stage"],
        "horizon_s": e["horizon_s"],
        "step_s": e["step_s"],
        "n_steps": len(scenario_mod.time_grid(s)),
        "altitude_km": e["altitude_km"],
        "inclination_deg": e["inclination_deg"],
        "min_elevation_deg": e["min_elevation_deg"],
        "isl_range_km": e["isl_range_km"],
        "target_availability": e["target_availability"],
        "n_failures": len(s["failures"]),
        "n_gateway_outages": len(s["gateway_outages"]),
    }


# ─────────────────────────────────── контекст соединения ───────────────────────────────────

@dataclass(frozen=True)
class Ctx:
    envelope_id: str
    send_text: Callable[[str], Awaitable[None]]
    send_bytes: Callable[[bytes], Awaitable[None]]


# ──────────────────────────────────── обработчики типов ────────────────────────────────────

async def _handle_scenario_load(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    # payload — сырой JSON сценария целиком (не обёрнут в {"scenario": ...}), см. protocol.py.
    s = scenario_mod.load_scenario(payload)
    variant_id = scenario_mod.scenario_hash(s)
    _STORE.save(variant_id, s, {
        "title": str(s.get("meta", {}).get("title") or "Загруженный сценарий"),
        "source": "load",
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    })
    return {
        "variant_id": variant_id,
        "scenario_hash": variant_id,
        "effective_scenario": scenario_mod.export_scenario(s),
        "summary": _summary(s),
    }


async def _handle_scenario_patch(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    parent_id, base = _require_variant(payload)
    patch = {k: v for k, v in payload.items() if k != "variant_id"}
    patched = scenario_mod.patch_scenario(base, patch)
    variant_id = scenario_mod.scenario_hash(patched)
    _STORE.save(variant_id, patched, {
        "title": str(patched.get("meta", {}).get("title") or "Правка конфигурации"),
        "source": "patch",
        "parent_variant_id": parent_id,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    })
    return {
        "variant_id": variant_id,
        "scenario_hash": variant_id,
        "parent_variant_id": parent_id,
        "effective_scenario": scenario_mod.export_scenario(patched),
        "summary": _summary(patched),
    }


async def _handle_compute(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any] | None:
    variant_id, _s = _require_variant(payload)
    strategy = _require_strategy(payload)

    async def on_progress(pct: int, stage: str) -> None:
        await ctx.send_text(protocol.dumps(protocol.progress_envelope(
            ctx.envelope_id, {"variant_id": variant_id, "strategy": strategy, "pct": pct, "stage": stage},
        )))

    result = await _ensure_computed(variant_id, strategy, on_progress)
    await ctx.send_text(protocol.dumps(protocol.result_envelope(ctx.envelope_id, "compute", result.manifest)))
    await ctx.send_bytes(result.binary)
    return None


async def _handle_attach(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any] | None:
    variant_id, _s = _require_variant(payload)
    strategy = _require_strategy(payload)
    result = _STORE.results.get((variant_id, strategy))
    if result is None:
        raise protocol.ProtocolError(
            "not_computed", "payload.variant_id",
            f"для варианта {variant_id!r} (strategy={strategy!r}) ещё нет готового расчёта — "
            "attach не пересчитывает, сначала отправьте compute",
        )
    await ctx.send_text(protocol.dumps(protocol.result_envelope(ctx.envelope_id, "attach", result.manifest)))
    await ctx.send_bytes(result.binary)
    return None


_MISS = object()


async def _handle_snapshot(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    variant_id, s = _require_variant(payload)
    strategy = _require_strategy(payload)
    t_s = _require_t_s(payload, s)

    snap = network.snapshot(s, t_s)
    sat_ids_set, _ground_ids = network.split_node_kinds(s)
    clients = sorted(g["id"] for g in s["ground_sites"] if g["role"] == "client")
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}

    cached = _STORE.results.get((variant_id, strategy))
    adj_plain: dict[str, set[str]] | None = None
    routes_out: list[dict[str, Any]] = []
    for client in clients:
        path = cached.routes_index.get((t_s, client), _MISS) if cached is not None else _MISS
        if path is _MISS:
            if adj_plain is None:
                adj_plain = network.build_adjacency(snap)
            route_adj = adj_plain if strategy == "hops" else routing.build_weighted_adjacency(snap)
            path = routing.find_route(route_adj, client, gateways, sat_ids_set, strategy=strategy)
        path = path or []
        reason = None
        if not path:
            if adj_plain is None:
                adj_plain = network.build_adjacency(snap)
            reason = routing.no_route_reason(s, t_s, adj_plain, client, gateways, sat_ids_set)
        routes_out.append({
            "client_id": client,
            "gateway_id": path[-1] if path else None,
            "path": path,
            "hops": (len(path) - 1) if path else None,
            "reason": reason,
        })

    return {
        "variant_id": variant_id,
        "t_s": t_s,
        "satellites": snap["satellites"],
        "isl_edges": snap["isl_edges"],
        "ground_contacts": snap["ground_contacts"],
        "routes": routes_out,
    }


_COMPARABLE_FIELDS: tuple[str, ...] = ("altitude_km", "isl_range_km", "min_elevation_deg", "step_s")
_ENV_DIFF_FIELDS: tuple[str, ...] = (
    "altitude_km", "inclination_deg", "earth_angle0_deg", "horizon_s",
    "step_s", "min_elevation_deg", "isl_range_km", "target_availability",
)


async def _handle_compare(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    variant_id_a, s_a = _require_variant(payload, "variant_id_a")
    variant_id_b, s_b = _require_variant(payload, "variant_id_b")
    strategy = _require_strategy(payload)

    result_a = await _ensure_computed(variant_id_a, strategy)
    result_b = await _ensure_computed(variant_id_b, strategy)

    env_a, env_b = s_a["environment"], s_b["environment"]
    mismatched = [f for f in _COMPARABLE_FIELDS if env_a[f] != env_b[f]]
    comparable = not mismatched
    warning = None
    if mismatched:
        warning = (
            "сравнение некорректно: у вариантов различаются параметры, обязательные для "
            f"сопоставимости (docs/PARAMETERS.md) — {', '.join(mismatched)}. Числа ниже посчитаны "
            "честно, но выводы «вариант B лучше A» по ним делать нельзя."
        )

    environment_diff = {
        f: {"a": env_a[f], "b": env_b[f]} for f in _ENV_DIFF_FIELDS if env_a[f] != env_b[f]
    }

    design_diff: dict[str, Any] = {}
    if s_a["design"]["launch_stage"] != s_b["design"]["launch_stage"]:
        design_diff["launch_stage"] = {"a": s_a["design"]["launch_stage"], "b": s_b["design"]["launch_stage"]}

    planes_a = {p["id"]: p for p in s_a["design"]["planes"]}
    planes_b = {p["id"]: p for p in s_b["design"]["planes"]}
    plane_diff: dict[str, Any] = {}
    for pid in sorted(set(planes_a) & set(planes_b)):
        pa, pb = planes_a[pid], planes_b[pid]
        changes = {k: {"a": pa[k], "b": pb[k]} for k in ("raan_deg", "phase_deg") if pa[k] != pb[k]}
        if changes:
            plane_diff[pid] = changes
    if plane_diff:
        design_diff["planes"] = plane_diff
    only_a = sorted(set(planes_a) - set(planes_b))
    only_b = sorted(set(planes_b) - set(planes_a))
    if only_a:
        design_diff["planes_only_in_a"] = only_a
    if only_b:
        design_diff["planes_only_in_b"] = only_b
    if len(s_a["failures"]) != len(s_b["failures"]):
        design_diff["n_failures"] = {"a": len(s_a["failures"]), "b": len(s_b["failures"])}

    clients_a = set(result_a.manifest["clients"])
    clients_b = set(result_b.manifest["clients"])
    common_clients = sorted(clients_a & clients_b)
    clients_out: dict[str, Any] = {}
    for c in common_clients:
        ma = result_a.manifest["client_metrics"][c]
        mb = result_b.manifest["client_metrics"][c]
        clients_out[c] = {
            "a": ma,
            "b": mb,
            "delta": {
                "vis_pct": mb["vis_pct"] - ma["vis_pct"],
                "avail_pct": mb["avail_pct"] - ma["avail_pct"],
                "max_gap_s": mb["max_gap_s"] - ma["max_gap_s"],
            },
        }

    return {
        "variant_id_a": variant_id_a,
        "variant_id_b": variant_id_b,
        "strategy": strategy,
        "comparable": comparable,
        "comparable_warning": warning,
        "environment_diff": environment_diff,
        "design_diff": design_diff,
        "clients": clients_out,
        "clients_only_in_a": sorted(clients_a - clients_b),
        "clients_only_in_b": sorted(clients_b - clients_a),
    }


async def _handle_analysis(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Анализ устойчивости сверх обязательных показателей.

    Обязательная метрика говорит «путь есть». Здесь считается то, чего по ней не видно и о чём
    прямо спрашивает критерий «Анализ устойчивости»: сколько отказов переживёт связь (резерв
    непересекающихся маршрутов), какие аппараты её рвут, чем ограничен резерв (кратность
    покрытия наземных линий) и что даёт терпимость к задержке доставки.

    Считается по запросу, а не при каждом `compute`: проход занимает единицы секунд, а нужен
    не на каждой перемотке. Результат кешируется по варианту.
    """
    variant_id, s = _require_variant(payload)

    cached = _ANALYSIS_CACHE.get(variant_id)
    if cached is not None:
        return cached

    def work() -> dict[str, Any]:
        return {
            "variant_id": variant_id,
            "reserve": metrics.reserve_profile(s),
            "critical_satellites": metrics.critical_satellites(s)[:12],
            "coverage": metrics.coverage_multiplicity(s),
            "delivery": delivery.delivery(s),
        }

    # Тот же запрет, что и для `compute`: длинный numpy-проход в корутине заблокировал бы
    # event loop, и соединение перестало бы отвечать (.claude/rules/protocol.md).
    result = await asyncio.to_thread(work)
    _ANALYSIS_CACHE[variant_id] = result
    return result


async def _handle_variants_list(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Список сохранённых вариантов.

    Нужен интерфейсу, чтобы сравнение предлагало выбор из уже загруженных конфигураций, а не
    только из тех, что появились в текущей вкладке браузера. Варианты лежат на диске и переживают
    перезапуск сервиса, поэтому список одинаков для всех подключений.
    """
    return {"variants": _STORE.listing()}


async def _handle_export(payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    variant_id, s = _require_variant(payload)
    kind = payload.get("kind", "result")
    if kind not in ("result", "scenario"):
        raise protocol.ProtocolError(
            "bad_request", "payload.kind", f"kind должен быть 'result' или 'scenario', получено {kind!r}",
        )
    if kind == "scenario":
        # Отдельная выгрузка изменённого сценария (cosmo-A-1.0) для повторной загрузки —
        # НЕ то же самое, что выгрузка результата расчёта (docs/PARAMETERS.md §6).
        return scenario_mod.export_scenario(s)

    strategy = _require_strategy(payload)
    result = await _ensure_computed(variant_id, strategy)
    return {
        "schema_version": "cosmo-A-result-1.0",
        "effective_scenario": scenario_mod.export_scenario(s),
        "routes": result.computed["routes"],
    }


_HANDLERS: dict[str, Callable[[dict[str, Any], Ctx], Awaitable[dict[str, Any] | None]]] = {
    "scenario.load": _handle_scenario_load,
    "scenario.patch": _handle_scenario_patch,
    "compute": _handle_compute,
    "snapshot": _handle_snapshot,
    "compare": _handle_compare,
    "export": _handle_export,
    "attach": _handle_attach,
    "analysis": _handle_analysis,
    "variants.list": _handle_variants_list,
}


# ────────────────────────────────────── диспетчер ───────────────────────────────────────────

async def _dispatch(envelope_id: str, msg_type: str, payload: dict[str, Any], ctx: Ctx) -> None:
    handler = _HANDLERS[msg_type]
    try:
        result_payload = await handler(payload, ctx)
    except protocol.ProtocolError as exc:
        await ctx.send_text(protocol.dumps(protocol.error_envelope(envelope_id, exc.code, exc.field, exc.message)))
        return
    except scenario_mod.ScenarioError as exc:
        await ctx.send_text(protocol.dumps(
            protocol.error_envelope(envelope_id, "validation_error", exc.field, exc.message)
        ))
        return
    except Exception as exc:  # noqa: BLE001 — последний рубеж: клиент получает internal_error, а не обрыв
        _LOG.exception("необработанная ошибка при обработке %s (id=%s)", msg_type, envelope_id)
        await ctx.send_text(protocol.dumps(
            protocol.error_envelope(envelope_id, "internal_error", "$", f"внутренняя ошибка сервера: {exc}")
        ))
        return
    if result_payload is not None:
        await ctx.send_text(protocol.dumps(protocol.result_envelope(envelope_id, msg_type, result_payload)))


# ───────────────────────────────────── сам эндпоинт ─────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Единственный эндпоинт сервиса. Сообщения одного соединения обрабатываются
    последовательно — одно полностью завершается (включая возможный бинарный фрейм) прежде,
    чем читается следующее. Это простое и достаточное решение: `to_thread` внутри обработчика
    уже не даёт долгому расчёту заблокировать другие СОЕДИНЕНИЯ (event loop свободен всё время
    ожидания потока), а последовательность в рамках одного сокета исключает переплетение
    двух пар «манифест + бинарный фрейм» на проводе, если бы два тяжёлых запроса с одного
    клиента совпали по времени."""
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                await websocket.send_text(protocol.dumps(protocol.error_envelope(
                    None, "bad_envelope", "$", "ожидался текстовый JSON-фрейм, получен бинарный",
                )))
                continue
            try:
                envelope_id, msg_type, payload = protocol.parse_message(text)
            except protocol.ProtocolError as exc:
                await websocket.send_text(protocol.dumps(
                    protocol.error_envelope(exc.envelope_id, exc.code, exc.field, exc.message)
                ))
                continue
            ctx = Ctx(envelope_id=envelope_id, send_text=websocket.send_text, send_bytes=websocket.send_bytes)
            await _dispatch(envelope_id, msg_type, payload, ctx)
    except WebSocketDisconnect:
        pass
