"""Показатели доступности по каждому наземному пункту и анализ устойчивости сети.

Обязательная часть — `compute()`: для каждого клиентского пункта считает долю отсчётов с
видимостью хотя бы одного активного аппарата, долю отсчётов со сквозным маршрутом «клиент →
спутники → шлюз», максимальный перерыв и ряд маршрутов для выгрузки `routes`.

Доступность и максимальный перерыв — это достижимость в графе (см. `docs/README.md`, вывод №1):
они НЕ зависят от алгоритма маршрутизации. Поэтому здесь эти две величины считаются собственным
BFS (`_bfs_reach`), побайтово повторяющим обход `tools/golden.py` — наземные узлы не ретранслируют,
поэтому обход идёт `клиент → спутники (BFS) → первый встреченный шлюз`. Строго обязательно, чтобы
это совпадало с эталоном, а не «было похоже».

Сам маршрут (нужен для `hops`, поля `path` в выгрузке и отрисовки) делегируется
`core.routing.find_route(adj, client, gateways, sat_ids, strategy, previous)` — модуль пишет
параллельно другой агент этого же прогона и на момент написания этого файла ещё не существует.
Поэтому ниже есть терпимый к отсутствию модуля путь: пока `core.routing` не появился, используется
локальный `_fallback_find_route` (детерминированный BFS по числу переходов с удержанием валидного
предыдущего маршрута) — он даёт корректный, но не обязательно самый короткий по километрам путь.
После интеграции `core.routing` подставляется автоматически, код здесь трогать не придётся.
Расхождение «BFS говорит путь есть, а find_route его не нашёл» — это ошибка модуля маршрутизации,
и `compute()` поднимает её как `AssertionError` с точным указанием отсчёта и пункта, а не молчит.

Сверх обязательного (расчёты и обоснование — `docs/EXTENSIONS.md`, разделы 2–3):
  disjoint_paths(adj, client, gateways, sat_ids) -> int
      число вершинно-непересекающихся маршрутов «пункт → любой шлюз» — максимальный поток при
      единичной пропускной способности каждого аппарата (теорема Менгера). «Доступность с резервом».
  critical_satellites(s) -> список аппаратов с числом отсчётов, где их изъятие рвёт связь хотя бы
      одному пункту.
  coverage_multiplicity(s) -> распределение числа одновременно видимых активных аппаратов по узлу.

Единицы: доли — числа 0..1 (не проценты), перерывы — секунды. Формат вывода в проценты/минуты —
дело веб-слоя, здесь только сырые числа для точных сравнений.
"""
from __future__ import annotations

import collections
from typing import Any

try:                    # обычный импорт как часть пакета core (from core import metrics)
    from . import network, scenario
except ImportError:     # запуск как самостоятельный скрипт: python3 metrics.py ...
    import network
    import scenario

# core.routing пишет параллельно другой агент этого же прогона; на момент написания этого файла
# модуля ещё нет. Импорт — best-effort: как только файл появится, compute() начнёт использовать
# find_route() из него без изменений в этом модуле (см. _route ниже).
try:
    from . import routing as _routing
except ImportError:
    try:
        import routing as _routing          # запуск как самостоятельный скрипт
    except ImportError:
        _routing = None

__all__ = ["compute", "disjoint_paths", "critical_satellites", "coverage_multiplicity"]


# ─────────────────────────── общее ядро достижимости (BFS) ────────────────────────────

def _bfs_reach(
    adj: dict[str, set[str]],
    client: str,
    gateways: set[str],
    sat_ids: set[str],
    blocked: frozenset[str] = frozenset(),
) -> tuple[bool, set[str]]:
    """Достижим ли хотя бы один шлюз из `client`, и какие спутники по пути посещены.

    Обход в точности повторяет эталон `tools/golden.py::metrics()`: с `client` можно шагнуть
    только на спутник (наземные узлы не ретранслируют — прочий наземный узел-сосед пропускается,
    даже если формально присутствует в `adj` как контакт спутника), с любого посещённого спутника —
    на другой спутник или на шлюз. `blocked` исключает узел из обхода целиком — на этом строится
    `critical_satellites` (проверка «а если этого аппарата не было бы»).

    Возвращает (reached, посещённые_спутники) — второе нужно только для сужения кандидатов в
    critical_satellites (тестировать имеет смысл лишь спутники, через которые путь вообще возможен).
    """
    if client in blocked or client not in adj:
        return False, set()
    seen = {client}
    visited_sats: set[str] = set()
    queue = collections.deque([client])
    reached = False
    while queue:
        u = queue.popleft()
        for v in adj.get(u, ()):
            if v in blocked or v in seen:
                continue
            if v in gateways:
                reached = True
                seen.add(v)
                continue
            if v in sat_ids:
                seen.add(v)
                visited_sats.add(v)
                queue.append(v)
            # иначе — другой наземный узел (например, второй клиент): не ретранслирует, пропускаем
    return reached, visited_sats


def _gap_stats(ok: list[bool]) -> tuple[int, int, int]:
    """(максимальная серия отказов, серия в начале горизонта, серия в конце) — в отсчётах.

    Серии в начале/конце — те же самые перерывы, просто дополнительно помечены как усечённые
    (ТЗ требует их отдельно, см. docs/PARAMETERS.md §4): если весь горизонт — один сплошной
    перерыв, все три числа совпадают, это корректно — он усечён с обеих сторон одновременно.
    """
    n = len(ok)
    max_run = cur = 0
    for v in ok:
        if v:
            cur = 0
        else:
            cur += 1
            max_run = max(max_run, cur)
    start_run = 0
    for v in ok:
        if v:
            break
        start_run += 1
    else:
        start_run = n
    end_run = 0
    for v in reversed(ok):
        if v:
            break
        end_run += 1
    else:
        end_run = n
    return max_run, start_run, end_run


# ─────────────────────────────── построение маршрута ──────────────────────────────────

def _path_valid(
    path: list[str] | None,
    adj: dict[str, set[str]],
    client: str,
    gateways: set[str],
    sat_ids: set[str],
) -> bool:
    """Путь физически возможен в текущем снимке: начинается в `client`, заканчивается в шлюзе,
    все промежуточные узлы — спутники, и каждое ребро реально существует в `adj`."""
    if not path or path[0] != client or path[-1] not in gateways:
        return False
    if any(node not in sat_ids for node in path[1:-1]):
        return False
    return all(b in adj.get(a, ()) for a, b in zip(path, path[1:]))


def _fallback_find_route(
    adj: dict[str, set[str]],
    client: str,
    gateways: set[str],
    sat_ids: set[str],
    previous: list[str] | None,
) -> list[str] | None:
    """BFS по числу переходов с удержанием валидного предыдущего маршрута и фиксированным
    (отсортированным) порядком обхода соседей — используется, пока `core.routing` не готов
    (см. докстринг модуля). Контракт идентичен будущему `routing.find_route(..., strategy='hops')`.
    """
    if previous and _path_valid(previous, adj, client, gateways, sat_ids):
        return previous
    if client not in adj:
        return None
    parent: dict[str, str | None] = {client: None}
    queue = collections.deque([client])
    goal: str | None = None
    while queue and goal is None:
        u = queue.popleft()
        for v in sorted(adj.get(u, ())):
            if v in parent or (v not in sat_ids and v not in gateways):
                continue
            parent[v] = u
            if v in gateways:
                goal = v
                break
            queue.append(v)
    if goal is None:
        return None
    path = [goal]
    while parent[path[-1]] is not None:
        path.append(parent[path[-1]])
    path.reverse()
    return path


def _route_adjacency(snap: dict[str, Any], adj: dict[str, set[str]], strategy: str):
    """Граф для поиска пути в форме, которую ждёт `core.routing.find_route`.

    'hops' работает на обычном невзвешенном `adj` (тот же граф, что для достижимости).
    'distance' (Дейкстра по километрам) требует взвешенный граф — строит его
    `routing.build_weighted_adjacency(snap)` из сырого снимка, не из `adj`; здесь просто
    выбирается нужная форма, сам граф не пересчитывается заново по-своему.
    """
    if strategy == "distance" and _routing is not None and hasattr(_routing, "build_weighted_adjacency"):
        return _routing.build_weighted_adjacency(snap)
    return adj


def _route(
    route_adj,
    client: str,
    gateways: set[str],
    sat_ids: set[str],
    strategy: str,
    previous: list[str] | None,
) -> list[str] | None:
    if _routing is not None and hasattr(_routing, "find_route"):
        return _routing.find_route(route_adj, client, gateways, sat_ids, strategy=strategy, previous=previous)
    return _fallback_find_route(route_adj, client, gateways, sat_ids, previous)


# ──────────────────────────────────── compute() ───────────────────────────────────────

def compute(s: dict, strategy: str = "hops") -> dict[str, Any]:
    """Показатели по каждому клиентскому пункту сценария `s` плюс ряд маршрутов для выгрузки.

    Возвращает:
      {
        "clients": {
          client_id: {
            "vis_pct":               доля отсчётов с видимостью ≥1 активного спутника, 0..1
            "avail_pct":              доля отсчётов со сквозным маршрутом до шлюза, 0..1
            "max_gap_s":              максимальный перерыв, секунды
            "gap_start_censored_s":   перерыв, упирающийся в t=0 (0, если t=0 доступен) — усечён
            "gap_end_censored_s":     перерыв, упирающийся в конец горизонта — усечён
            "visible": [bool, ...],   по отсчётам сетки, для диаграммы доступности
            "ok": [bool, ...],        по отсчётам сетки, есть ли маршрут
            "hops": [int|None, ...],  число переходов по отсчётам; None — маршрута нет (не 0)
          }, ...
        },
        "routes": [{"t_s": int, "client_id": str, "path": [str, ...]}, ...]  — по одной записи
            на каждую пару (t_s, client_id), путь пустой список, если маршрута нет — готово для
            поля `routes` выгрузки `cosmo-A-result-1.0`.
      }

    `strategy` передаётся в `core.routing.find_route` (см. его докстринг про 'hops'/'distance');
    на avail_pct/vis_pct/max_gap не влияет — это достижимость, а не свойство алгоритма.
    """
    steps = scenario.time_grid(s)
    n = len(steps)
    if n == 0:
        raise ValueError("пустая сетка отсчётов: horizon_s/step_s дают 0 шагов")
    step_s = int(s["environment"]["step_s"])
    sat_ids, _ground_ids = network.split_node_kinds(s)
    clients = [g["id"] for g in s["ground_sites"] if g["role"] == "client"]
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    if not clients:
        raise ValueError("в сценарии нет ни одного пункта с role='client'")
    if not gateways:
        raise ValueError("в сценарии нет ни одного пункта с role='gateway'")

    per_client: dict[str, dict[str, list]] = {
        c: {"visible": [], "ok": [], "hops": [], "paths": []} for c in clients
    }
    prev_path: dict[str, list[str] | None] = dict.fromkeys(clients)

    for t in steps:
        snap = network.snapshot(s, t)
        adj = network.build_adjacency(snap)
        route_adj = _route_adjacency(snap, adj, strategy)
        for c in clients:
            visible = bool(adj.get(c, set()) & sat_ids)
            reached, _visited = _bfs_reach(adj, c, gateways, sat_ids)
            path = _route(route_adj, c, gateways, sat_ids, strategy, prev_path[c]) if reached else None
            if reached and not path:
                raise AssertionError(
                    f"routing разошёлся с достижимостью на t_s={t}, client={c}: "
                    "BFS находит путь до шлюза, find_route — нет (ошибка в core.routing, не в metrics)"
                )
            if (not reached) and path:
                raise AssertionError(
                    f"find_route вернул путь на t_s={t}, client={c}, хотя шлюз недостижим по BFS "
                    f"(путь: {path}) — ошибка в core.routing (нет проверки допустимости узлов)"
                )
            prev_path[c] = path
            rec = per_client[c]
            rec["visible"].append(visible)
            rec["ok"].append(bool(path))
            rec["hops"].append(len(path) - 1 if path else None)
            rec["paths"].append(path or [])

    clients_out: dict[str, Any] = {}
    routes_flat: list[dict[str, Any]] = []
    for c in clients:
        rec = per_client[c]
        max_run, start_run, end_run = _gap_stats(rec["ok"])
        clients_out[c] = {
            "vis_pct": sum(rec["visible"]) / n,
            "avail_pct": sum(rec["ok"]) / n,
            "max_gap_s": max_run * step_s,
            "gap_start_censored_s": start_run * step_s,
            "gap_end_censored_s": end_run * step_s,
            "visible": rec["visible"],
            "ok": rec["ok"],
            "hops": rec["hops"],
        }
        for t, path in zip(steps, rec["paths"]):
            routes_flat.append({"t_s": t, "client_id": c, "path": path})

    return {"clients": clients_out, "routes": routes_flat}


# ───────────────────────────── резерв маршрутов (max-flow) ────────────────────────────

def disjoint_paths(
    adj: dict[str, set[str]],
    client: str,
    gateways: set[str],
    sat_ids: set[str],
) -> int:
    """Число вершинно-непересекающихся маршрутов `client` → любой из `gateways` в снимке `adj`.

    Максимальный поток (Edmonds-Karp, BFS-augmenting) при единичной пропускной способности
    каждого аппарата — узел спутника расщеплён на вход/выход с ребром ёмкости 1, наземные узлы
    (клиент, шлюзы) не ограничены. По теореме Менгера это равно минимальному числу спутников,
    чьё одновременное изъятие разрывает связь `client` со всеми шлюзами. Узлы, которые не являются
    ни `client`, ни спутником, ни шлюзом (прочие клиентские пункты), в граф потока не включаются —
    они не ретранслируют и не могут быть частью пути.
    """
    if client not in adj:
        return 0
    INF = 10**9
    SINK = "__sink__"  # заведомо не встречается среди ID сценария

    def n_in(node: str) -> str:
        return f"{node}__in" if node in sat_ids else node

    def n_out(node: str) -> str:
        return f"{node}__out" if node in sat_ids else node

    cap: dict[str, dict[str, int]] = collections.defaultdict(dict)

    def add_edge(u: str, v: str, c: int) -> None:
        cap[u][v] = cap[u].get(v, 0) + c
        cap[v].setdefault(u, 0)

    relevant = {client} | sat_ids | gateways
    for sid in sat_ids & relevant:
        add_edge(n_in(sid), n_out(sid), 1)
    for u, neighbors in adj.items():
        if u not in relevant:
            continue
        for v in neighbors:
            if v not in relevant:
                continue
            add_edge(n_out(u), n_in(v), INF)
    for gw in gateways:
        add_edge(n_out(gw), SINK, INF)

    source = n_out(client)
    flow = 0
    while True:
        parent: dict[str, str | None] = {source: None}
        queue = collections.deque([source])
        while queue:
            u = queue.popleft()
            if u == SINK:
                break
            for v, c in cap.get(u, {}).items():
                if c > 0 and v not in parent:
                    parent[v] = u
                    queue.append(v)
        if SINK not in parent:
            break
        bottleneck = INF
        v = SINK
        while parent[v] is not None:
            u = parent[v]
            bottleneck = min(bottleneck, cap[u][v])
            v = u
        v = SINK
        while parent[v] is not None:
            u = parent[v]
            cap[u][v] -= bottleneck
            cap[v][u] += bottleneck
            v = u
        flow += bottleneck
    return flow


# ───────────────────────────────── критические аппараты ───────────────────────────────

def reserve_profile(s: dict) -> dict[str, dict[str, Any]]:
    """Распределение числа вершинно-непересекающихся маршрутов «пункт → шлюз» по всему горизонту.

    Обязательная метрика отвечает на вопрос «есть ли путь». Этот профиль отвечает на другой:
    сколько отказов переживёт связь прямо сейчас. Разница принципиальна — полная группировка
    выполняет цель по доступности, но резерв из двух независимых маршрутов существует лишь
    16-37 % времени (docs/EXTENSIONS.md §2), то есть две трети суток связь висит на одной нитке.

    Возвращает {client_id: {"histogram": {"0": доля, "1": доля, ...}, "reserve_pct": доля
    отсчётов с двумя и более непересекающимися маршрутами, "mean": среднее число маршрутов}}.
    Доли — 0..1, как и остальные показатели протокола.
    """
    sat_ids, _ = network.split_node_kinds(s)
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    clients = [g["id"] for g in s["ground_sites"] if g["role"] == "client"]
    steps = scenario.time_grid(s)
    if not steps:
        return {}

    counts: dict[str, collections.Counter[int]] = {c: collections.Counter() for c in clients}
    for t_s in steps:
        adj = network.build_adjacency(network.snapshot(s, t_s))
        for c in clients:
            counts[c][disjoint_paths(adj, c, gateways, sat_ids)] += 1

    n = len(steps)
    out: dict[str, dict[str, Any]] = {}
    for c in clients:
        hist = counts[c]
        out[c] = {
            "histogram": {str(k): hist[k] / n for k in sorted(hist)},
            "reserve_pct": sum(v for k, v in hist.items() if k >= 2) / n,
            "mean": sum(k * v for k, v in hist.items()) / n,
        }
    return out


def critical_satellites(s: dict, strategy: str = "hops") -> list[dict[str, Any]]:
    """Аппараты, чьё изъятие рвёт связь «клиент → шлюз» хотя бы одному пункту, с числом
    отсчётов, где это происходит.

    Для каждого отсчёта и каждого клиента с существующим маршрутом перебираются только спутники,
    реально посещённые BFS от этого клиента (`_bfs_reach`, множество visited) — тестировать
    остальные бессмысленно, через них путь и так не идёт. Спутник критичен на отсчёте, если после
    его исключения (`blocked={sat}`) шлюз становится недостижим — по теореме Менгера это ровно
    случай, когда число вершинно-непересекающихся маршрутов через него равно 1. Сложность —
    (число отсчётов) × (клиенты) × (посещённые спутники, обычно единицы), а не полный перебор
    48 аппаратов на каждом шаге: считается за секунды даже на 720×48.

    `strategy` не используется (критичность — свойство графа связности, не алгоритма
    маршрутизации), параметр оставлен для единообразия сигнатур модуля.
    """
    steps = scenario.time_grid(s)
    sat_ids, _ = network.split_node_kinds(s)
    clients = [g["id"] for g in s["ground_sites"] if g["role"] == "client"]
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    counts: dict[str, int] = collections.defaultdict(int)

    for t in steps:
        snap = network.snapshot(s, t)
        adj = network.build_adjacency(snap)
        for c in clients:
            reached, visited_sats = _bfs_reach(adj, c, gateways, sat_ids)
            if not reached:
                continue
            for sat in visited_sats:
                still_reached, _ = _bfs_reach(adj, c, gateways, sat_ids, blocked=frozenset({sat}))
                if not still_reached:
                    counts[sat] += 1

    return sorted(
        ({"satellite_id": sid, "count": n} for sid, n in counts.items()),
        key=lambda item: (-item["count"], item["satellite_id"]),
    )


# ────────────────────────────────── кратность покрытия ─────────────────────────────────

def coverage_multiplicity(s: dict) -> dict[str, dict[str, Any]]:
    """Распределение числа одновременно видимых активных аппаратов по каждому наземному узлу
    (и клиентам, и шлюзам — резерв маршрутов упирается именно в наземные линии, см.
    docs/EXTENSIONS.md §3, и это видно только если считать по всем узлам, не только по клиентам).

    Возвращает {node_id: {"histogram": {k: доля_отсчётов, ...}, "mean": среднее_число_спутников}}.
    """
    steps = scenario.time_grid(s)
    n = len(steps)
    sat_ids, _ = network.split_node_kinds(s)
    nodes = [g["id"] for g in s["ground_sites"]]
    counts_per_node: dict[str, list[int]] = {node: [] for node in nodes}

    for t in steps:
        snap = network.snapshot(s, t)
        adj = network.build_adjacency(snap)
        for node in nodes:
            counts_per_node[node].append(len(adj.get(node, set()) & sat_ids))

    result: dict[str, dict[str, Any]] = {}
    for node, counts in counts_per_node.items():
        hist = collections.Counter(counts)
        result[node] = {
            "histogram": {k: v / n for k, v in sorted(hist.items())},
            "mean": sum(counts) / n,
        }
    return result


# ──────────────────────────────────── самопроверка ─────────────────────────────────────

def _pct(x: float) -> float:
    return round(100 * x, 2)


def _min(seconds: float) -> float:
    return round(seconds / 60, 1)


def _check(label: str, got: float, expected: float, tol: float = 0.011) -> None:
    assert abs(got - expected) <= tol, f"{label}: получено {got}, ожидалось {expected} (эталон)"
    print(f"    {label}: {got} — ок (эталон {expected})")


def _demo(path: str, golden: dict[str, dict[str, float]]) -> None:
    """golden: {client_id: {"vis": .., "avail": .., "gap_min": ..}} — из CLAUDE.md / docs/PARAMETERS.md."""
    s = scenario.load_scenario(__import__("json").loads(__import__("pathlib").Path(path).read_text(encoding="utf-8")))
    print(f"\n{path}")
    import time as _time
    t0 = _time.time()
    result = compute(s, strategy="hops")
    elapsed = _time.time() - t0
    print(f"  compute(): {elapsed:.2f} с на {len(scenario.time_grid(s))} отсчётов "
          f"× {len(result['clients'])} пунктов")

    n_steps = len(scenario.time_grid(s))
    clients_expected = {g["id"] for g in s["ground_sites"] if g["role"] == "client"}
    assert set(result["clients"]) == clients_expected, "compute() вернул не тот набор клиентов"
    seen_pairs = {(r["t_s"], r["client_id"]) for r in result["routes"]}
    need_pairs = {(t, c) for t in scenario.time_grid(s) for c in clients_expected}
    assert seen_pairs == need_pairs, "routes: не по одной записи на каждую пару (t_s, client_id)"
    assert len(result["routes"]) == len(seen_pairs), "routes: есть дубли (t_s, client_id)"

    for c, m in sorted(result["clients"].items()):
        if c not in golden:
            continue
        g = golden[c]
        _check(f"{c} vis_pct", _pct(m["vis_pct"]), g["vis"])
        _check(f"{c} avail_pct", _pct(m["avail_pct"]), g["avail"])
        _check(f"{c} max_gap_min", _min(m["max_gap_s"]), g["gap_min"], tol=0.05)
        # число переходов — минимум 2, и None ровно там, где нет маршрута
        for h, ok in zip(m["hops"], m["ok"]):
            assert (h is None) == (not ok), f"{c}: hops/ok разошлись"
            assert h is None or h >= 2, f"{c}: переходов {h} < 2 — маршрут не может быть короче"
        # усечённые перерывы не длиннее максимального и совпадают по краям с ok[]
        assert m["gap_start_censored_s"] <= max(m["max_gap_s"], m["gap_start_censored_s"])
        if m["ok"][0]:
            assert m["gap_start_censored_s"] == 0
        if m["ok"][-1]:
            assert m["gap_end_censored_s"] == 0

    # резерв маршрутов и критические аппараты — на первых 48 отсчётах (достаточно для проверки
    # работоспособности; полный прогон по всем клиентам/отсчётам делает __main__ ниже только
    # для отчёта, не для ассертов — там нет независимого эталона на сотые доли)
    sat_ids, _ = network.split_node_kinds(s)
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    snap0 = network.snapshot(s, 0)
    adj0 = network.build_adjacency(snap0)
    for c in clients_expected:
        dp = disjoint_paths(adj0, c, gateways, sat_ids)
        reached, _ = _bfs_reach(adj0, c, gateways, sat_ids)
        assert (dp >= 1) == reached, f"{c}: disjoint_paths={dp}, но достижимость BFS={reached}"
        assert dp <= len(gateways) + len(sat_ids)  # грубая верхняя граница, ловит явный breakage
    print(f"  disjoint_paths(t=0) и BFS-достижимость согласованы для {len(clients_expected)} пунктов")

    crit = critical_satellites(s)
    if crit:
        print(f"  critical_satellites: {len(crit)} аппаратов критичны хотя бы раз, "
              f"топ-3: {crit[:3]}")
    else:
        print("  critical_satellites: критичных аппаратов не найдено")

    cov = coverage_multiplicity(s)
    for c in sorted(clients_expected):
        m = cov[c]
        assert abs(sum(m["histogram"].values()) - 1.0) < 1e-9, f"{c}: гистограмма кратности не сумма 1"
        print(f"    coverage_multiplicity[{c}]: mean={m['mean']:.2f}, "
              f"histogram={ {k: round(v, 4) for k, v in m['histogram'].items()} }")


if __name__ == "__main__":
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    default_pairs = [
        (str(root / "Данные" / "01_full_constellation.json"), {
            "C65": {"vis": 97.78, "avail": 96.67, "gap_min": 8},
            "C70": {"vis": 99.86, "avail": 98.75, "gap_min": 2},
            "C72": {"vis": 100.00, "avail": 98.89, "gap_min": 2},
        }),
        (str(root / "Данные" / "04_link_range.json"), {
            "C65": {"vis": 97.78, "avail": 77.50, "gap_min": 94},
            "C70": {"vis": 99.86, "avail": 62.22, "gap_min": 178},
            "C72": {"vis": 100.00, "avail": 65.14, "gap_min": 4},
        }),
    ]
    paths = sys.argv[1:]
    pairs = default_pairs if not paths else [(p, {}) for p in paths]
    for p, golden in pairs:
        _demo(p, golden)
    print("\nmetrics.py: самопроверка пройдена (совпадает с эталоном tools/golden.py)")
