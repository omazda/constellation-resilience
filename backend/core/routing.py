"""Маршрут «клиентский пункт → спутники → шлюз» на снимке сети и причина его отсутствия.

Работает на снимке одного момента `t_s` — граф связности (`adj`) даёт вызывающий код
(`core.network.build_adjacency` для числа переходов, `build_weighted_adjacency` этого модуля
для километров). Сам модуль ничего не знает про геометрию орбит и не пересчитывает её —
кроме одного исключения: `no_route_reason` может дёрнуть `core.geometry.snapshot()` за
сырыми углами места, чтобы отличить «шлюз вне зоны» от «шлюз в отказе» (см. ниже).

КРИТИЧНО (docs/PARAMETERS.md, §4): наземный узел — только конец маршрута, никогда транзит.
В рёбрах снимка есть контакты «клиент ↔ спутник» для КАЖДОГО клиентского пункта, и наивный
обход графа охотно построит путь через второй клиентский пункт как через ретранслятор — это
завышает доступность до +11 п.п. на сценарии 04 (там, где выводы вообще делаются). Поэтому
и BFS, и Дейкстра здесь по построению никогда не кладут в очередь ничего, кроме id из
`sat_ids` (транзит) и `gateways` (цель) — id из «третьих» наземных узлов просто не проходят
фильтр расширения, даже если formально есть в `adj`.

Две стратегии дают ОДИНАКОВУЮ доступность — это достижимость в графе, от алгоритма
не зависит (docs/PARAMETERS.md §5). Отличаются длина и стабильность маршрута:

    стратегия           переходы (медиана)   длина, C72 (медиана)   смен пути (01, C72)
    hops (BFS)           2–3                  5007 км                599 из 712 (84 %)
    hops + удержание      2–3                  5287 км                450 из 712 (63 %)
    distance (Дейкстра)   2–3                  4602 км                547 из 712 (77 %)

Порядок обхода соседей всюду фиксируется сортировкой id — иначе два одинаковых запуска
(другой порядок хеширования строк в новом процессе) молча дают разные, но одинаково
«правильные» маршруты, и выгрузка перестаёт быть воспроизводимой.
"""
from __future__ import annotations

import collections
import heapq
import math
from typing import Any

try:                    # обычный импорт как часть пакета core (from core import routing)
    from . import geometry
except ImportError:     # запуск как самостоятельный скрипт: python3 routing.py ...
    import geometry


__all__ = ["REASONS", "find_route", "no_route_reason", "build_weighted_adjacency"]

# Ровно четыре причины отсутствия маршрута — прямо перечислены в ТЗ (docs/PARAMETERS.md §6).
REASONS = ("no_visible_satellite", "isl_partition", "no_gateway_contact", "gateway_offline")

# Узел графа снимка: либо {сосед, ...} (core.network.build_adjacency — для 'hops'),
# либо {сосед: расстояние_км, ...} (build_weighted_adjacency ниже — нужен для 'distance').
Adjacency = dict[str, "set[str] | dict[str, float]"]


def _neighbor_ids(adj: Adjacency, node: str) -> set[str]:
    """id соседей узла в этот момент — годится для любого из двух представлений `adj`."""
    raw = adj.get(node)
    if raw is None:
        return set()
    return set(raw.keys()) if isinstance(raw, dict) else set(raw)


def _path_valid(
    adj: Adjacency, path: list[str] | None, client: str, gateways: set[str], sat_ids: set[str]
) -> bool:
    """True, если `path` — всё ещё реальный маршрут на текущем `adj`: начинается в `client`,
    заканчивается в одном из `gateways`, все промежуточные узлы — спутники, и каждое ребро
    пути присутствует в снимке сейчас (спутник мог с тех пор потерять контакт или отказать)."""
    if not path or len(path) < 2:
        return False
    if path[0] != client or path[-1] not in gateways:
        return False
    if any(node not in sat_ids for node in path[1:-1]):
        return False
    return all(b in _neighbor_ids(adj, a) for a, b in zip(path, path[1:]))


def _reconstruct_bfs(parent: dict[str, str], client: str, last_sat: str, gateway: str) -> list[str]:
    chain = [last_sat]
    cur = last_sat
    while parent[cur] != client:
        cur = parent[cur]
        chain.append(cur)
    chain.reverse()
    return [client, *chain, gateway]


def _bfs(adj: Adjacency, client: str, gateways: set[str], sat_ids: set[str]) -> list[str] | None:
    """Кратчайший по числу переходов маршрут. Очередь пополняется только спутниками —
    наземный узел никогда не становится промежуточным, см. докстринг модуля."""
    start = sorted(_neighbor_ids(adj, client) & sat_ids)
    if not start:
        return None
    visited = {client, *start}
    parent: dict[str, str] = {sat: client for sat in start}
    queue: collections.deque[str] = collections.deque(start)
    while queue:
        node = queue.popleft()
        neighbors = sorted(_neighbor_ids(adj, node))
        gateway_hit = next((n for n in neighbors if n in gateways), None)
        if gateway_hit is not None:
            return _reconstruct_bfs(parent, client, node, gateway_hit)
        for n in neighbors:
            if n in sat_ids and n not in visited:
                visited.add(n)
                parent[n] = node
                queue.append(n)
    return None


def _dijkstra(adj: Adjacency, client: str, gateways: set[str], sat_ids: set[str]) -> list[str] | None:
    """Кратчайший по сумме км маршрут. Требует взвешенного `adj` (build_weighted_adjacency) —
    на обычном dict[str, set[str]] явно падает, а не молча считает переходы километрами."""
    dist: dict[str, float] = {client: 0.0}
    parent: dict[str, str] = {}
    done: set[str] = set()
    heap: list[tuple[float, str]] = [(0.0, client)]
    goal: str | None = None
    while heap:
        d, node = heapq.heappop(heap)
        if node in done:
            continue
        done.add(node)
        if node in gateways:
            goal = node
            break
        raw = adj.get(node)
        if isinstance(raw, set):
            raise ValueError(
                "strategy='distance' требует взвешенного adj (dict[str, dict[str, float]] "
                "с расстояниями в км), получен узел с dict[str, set[str]] — постройте граф "
                "через build_weighted_adjacency(network.snapshot(...))"
            )
        for n, w in sorted((raw or {}).items()):
            if n == client or n in done or not (n in sat_ids or n in gateways):
                continue
            nd = d + w
            if nd < dist.get(n, math.inf) - 1e-9:
                dist[n] = nd
                parent[n] = node
                heapq.heappush(heap, (nd, n))
    if goal is None:
        return None
    chain = [goal]
    cur = goal
    while cur != client:
        cur = parent[cur]
        if cur != client:
            chain.append(cur)
    chain.reverse()
    return [client, *chain]


def find_route(
    adj: Adjacency,
    client: str,
    gateways: "set[str] | list[str]",
    sat_ids: "set[str] | list[str]",
    strategy: str = "hops",
    previous: list[str] | None = None,
) -> list[str] | None:
    """Маршрут «client → спутник(и) → один из gateways» на снимке сети `adj`, или `None`,
    если сейчас пути нет (тогда причину даёт `no_route_reason`).

    `strategy`:
      'hops'     — BFS по числу переходов (минимум переходов, длина в км не оптимальна).
      'distance' — Дейкстра по километрам (`adj` обязан быть взвешенным, см. `_dijkstra`).
    Обе — достижимость в графе, доступность и максимальный перерыв от выбора не зависят
    (docs/PARAMETERS.md §5); отличаются только форма маршрута.

    `previous` — маршрут с предыдущего отсчёта. Если он всё ещё физически валиден на текущем
    `adj` (см. `_path_valid`), возвращается БЕЗ пересчёта — это и есть «удержание», снимающее
    около пятой части перестроений (иначе путь меняется на 51–84 % отсчётов, docs/PARAMETERS.md).
    Инвалидный `previous` (спутник вышел из связи/отказал) молча игнорируется — ищется заново.
    """
    gateways = set(gateways)
    sat_ids = set(sat_ids)
    if previous and _path_valid(adj, previous, client, gateways, sat_ids):
        return list(previous)
    if strategy == "hops":
        return _bfs(adj, client, gateways, sat_ids)
    if strategy == "distance":
        return _dijkstra(adj, client, gateways, sat_ids)
    raise ValueError(f"неизвестная стратегия маршрутизации: {strategy!r} (ожидается 'hops' или 'distance')")


def _offline_gateway_in_range(
    s: dict, t_s: float, gateways: set[str], elevation_deg: dict[str, dict[str, float]] | None
) -> bool:
    """True, если хотя бы один из `gateways`, отключённый прямо сейчас `gateway_outages`,
    геометрически находился бы в зоне видимости (elevation >= min_elevation_deg) хотя бы
    одного активного аппарата — то есть маршруту помешал именно отказ шлюза, а не геометрия.

    Ленивая: если на этом `t_s` ни один шлюз не в отказе, `geometry.snapshot` не дёргается
    вовсе (на всех четырёх сценариях ТЗ `gateway_outages` пуст — эта ветка стоит 0 лишних
    расчётов геометрии в обычном прогоне; жюри может загрузить сценарий, где она сработает).
    """
    outages = s.get("gateway_outages", [])
    offline = {
        gw for gw in gateways
        if any(o["gateway_id"] == gw and o["start_s"] <= t_s < o["end_s"] for o in outages)
    }
    if not offline:
        return False
    if elevation_deg is None:
        # Сырые углы места по ВСЕМ активным аппаратам, БЕЗ фильтра по порогу/отказу шлюза —
        # то, чего нет в готовых рёбрах снимка (там contact уже подавлен из-за offline).
        elevation_deg = geometry.snapshot(s, t_s)["elevation_deg"]
    min_elev = s["environment"]["min_elevation_deg"]
    return any(
        el >= min_elev
        for gw in offline
        for el in elevation_deg.get(gw, {}).values()
    )


def no_route_reason(
    s: dict,
    t_s: float,
    adj: Adjacency,
    client: str,
    gateways: "set[str] | list[str]",
    sat_ids: "set[str] | list[str]",
    *,
    elevation_deg: dict[str, dict[str, float]] | None = None,
) -> str:
    """Почему для `client` на `adj` (снимок момента `t_s`) сейчас нет маршрута до `gateways`.
    Вызывается ПОСЛЕ того, как `find_route(adj, client, gateways, sat_ids, ...)` вернул `None`
    на ТЕХ ЖЕ `adj`/`gateways`/`sat_ids` — иначе результат не гарантирован.

    Одна из четырёх причин ТЗ (`REASONS`), по возрастанию «глубины» диагноза:

      no_visible_satellite — у пункта нет ни одного контакта со спутником прямо сейчас:
        проблема в покрытии (сценарий 02 — целиком об этом, 12–27 % доступности).
      isl_partition — спутник(и) видны, но межспутниковая сеть, достижимая от них, не
        дотягивается ни до одного шлюза, ХОТЯ где-то в группировке контакт со шлюзом в этот
        момент есть (иначе сработала бы одна из причин ниже) — значит сеть разбита на
        изолированные части, и пункт не в той части. Это и есть тезис «видимость ≠ маршрут»
        сценария 04: видимость 97.78–100 %, доступность на 20–35 п.п. ниже — из-за разрыва
        именно ISL, не покрытия.
      no_gateway_contact — контакта со шлюзом нет НИГДЕ в группировке прямо сейчас: ни один
        активный аппарат не в зоне видимости ни одного шлюза (геометрия, а не отказ шлюза).
      gateway_offline — геометрически контакт был бы (какой-то аппарат в зоне видимости
        шлюза по углу места), но сам шлюз в этот момент в `gateway_outages`.

    `elevation_deg` — опциональная сырая карта углов места {шлюз: {спутник: elevation_deg}}
    из уже посчитанного `geometry.snapshot(s, t_s)["elevation_deg"]`, если он уже под рукой
    у вызывающего кода (экономит повторный пересчёт геометрии на каждом «нет маршрута» шаге).
    Без неё используется только при реальном отказе шлюза (см. `_offline_gateway_in_range`).
    """
    gateways = set(gateways)
    sat_ids = set(sat_ids)

    direct = _neighbor_ids(adj, client) & sat_ids
    if not direct:
        return "no_visible_satellite"

    # Контакт со шлюзом хоть где-то в текущем снимке (не обязательно у ЭТОГО пункта): раз
    # find_route для client уже не нашёл пути, но такой контакт существует — до него не
    # дотянуться именно из-за разрыва ISL-сети, а не из-за отсутствия покрытия у шлюза.
    if any(_neighbor_ids(adj, gw) & sat_ids for gw in gateways):
        return "isl_partition"

    if _offline_gateway_in_range(s, t_s, gateways, elevation_deg):
        return "gateway_offline"

    return "no_gateway_contact"


def build_weighted_adjacency(snap: dict) -> dict[str, dict[str, float]]:
    """Взвешенная (расстояние в км) версия графа связности снимка — нужна strategy='distance'.

    Принимает объект вида `core.network.snapshot(s, t_s)`: списки `isl_edges`
    (`{a, b, dist_km}`) и `ground_contacts` (`{ground_id, satellite_id, dist_km}`).
    `core.network.build_adjacency` веса намеренно не хранит (не нужны большинству вызывающего
    кода — числу переходов, поиску резервных путей); здесь же вес обязателен, без него
    Дейкстра по километрам работать не может — см. `_dijkstra`.
    """
    adjacency: dict[str, dict[str, float]] = {}
    for edge in snap.get("isl_edges", []):
        a, b, d = edge["a"], edge["b"], float(edge["dist_km"])
        adjacency.setdefault(a, {})[b] = d
        adjacency.setdefault(b, {})[a] = d
    for contact in snap.get("ground_contacts", []):
        g, sat, d = contact["ground_id"], contact["satellite_id"], float(contact["dist_km"])
        adjacency.setdefault(g, {})[sat] = d
        adjacency.setdefault(sat, {})[g] = d
    return adjacency


# ──────────────────────────────────── самопроверка ─────────────────────────────────────

def _check_synthetic() -> None:
    """Юнит-проверки на маленьких ручных графах — без реальной геометрии, чтобы гарантированно
    пройти все ветки, которые на четырёх сценариях ТЗ физически не встречаются (ни у одного
    файла нет `gateway_outages` — ветку gateway_offline на реальных данных не проверить)."""
    sat_ids = {"S1", "S2", "S3"}
    gateways = {"G1"}
    s_stub = {"environment": {"min_elevation_deg": 10.0}, "gateway_outages": []}

    # 1. нет видимого спутника
    adj = {"C1": set()}
    assert no_route_reason(s_stub, 0, adj, "C1", gateways, sat_ids) == "no_visible_satellite"
    assert find_route(adj, "C1", gateways, sat_ids) is None

    # 2. разрыв ISL: пункт видит S1, а G1 контактирует только с S3 — другая компонента сети
    adj = {"C1": {"S1"}, "S1": {"C1"}, "S3": {"G1"}, "G1": {"S3"}}
    assert no_route_reason(s_stub, 0, adj, "C1", gateways, sat_ids) == "isl_partition"
    assert find_route(adj, "C1", gateways, sat_ids) is None

    # 3. контакта со шлюзом нет нигде в группировке (и сам шлюз не в отказе)
    adj = {"C1": {"S1"}, "S1": {"C1", "S2"}, "S2": {"S1"}}
    assert no_route_reason(s_stub, 0, adj, "C1", gateways, sat_ids) == "no_gateway_contact"

    # 4. шлюз в отказе: геометрически контакт был бы (elevation выше порога), но outage его скрыл
    s_outage = {
        "environment": {"min_elevation_deg": 10.0},
        "gateway_outages": [{"gateway_id": "G1", "start_s": 0, "end_s": 3600}],
    }
    reason = no_route_reason(
        s_outage, 0, adj, "C1", gateways, sat_ids,
        elevation_deg={"G1": {"S1": 25.0, "S2": 5.0}},
    )
    assert reason == "gateway_offline", reason

    # 5. найденный маршрут структурно корректен, второй клиентский пункт не становится транзитом
    adj = {
        "C1": {"S1"}, "S1": {"C1", "S2", "C2"}, "C2": {"S1"},
        "S2": {"S1", "G1"}, "G1": {"S2"},
    }
    route = find_route(adj, "C1", gateways, sat_ids)
    assert route == ["C1", "S1", "S2", "G1"], route
    assert "C2" not in route

    # 6. удержание предыдущего маршрута, пока он валиден; невалидный — молча игнорируется
    kept = find_route(adj, "C1", gateways, sat_ids, previous=["C1", "S1", "S2", "G1"])
    assert kept == ["C1", "S1", "S2", "G1"]
    broken_previous = ["C1", "S1", "S3", "G1"]  # S3 не сосед S1 в этом снимке
    rebuilt = find_route(adj, "C1", gateways, sat_ids, previous=broken_previous)
    assert rebuilt == ["C1", "S1", "S2", "G1"]

    # 7. hops берёт минимум переходов, distance — минимум суммы км; на разных путях это разные маршруты
    weighted = {
        "C1": {"S1": 500.0, "S4": 500.0},
        "S1": {"C1": 500.0, "G1": 4000.0},
        "S4": {"C1": 500.0, "S5": 800.0},
        "S5": {"S4": 800.0, "G1": 800.0},
        "G1": {"S1": 4000.0, "S5": 800.0},
    }
    sat_ids2 = {"S1", "S4", "S5"}
    hops_route = find_route(weighted, "C1", gateways, sat_ids2, strategy="hops")
    dist_route = find_route(weighted, "C1", gateways, sat_ids2, strategy="distance")
    assert hops_route == ["C1", "S1", "G1"], hops_route            # 2 перехода
    assert dist_route == ["C1", "S4", "S5", "G1"], dist_route      # 2100 км против 4500 км

    # 8. distance на невзвешенном adj явно падает, а не молча считает переходы километрами
    plain = {"C1": {"S1"}, "S1": {"C1", "G1"}, "G1": {"S1"}}
    try:
        find_route(plain, "C1", gateways, {"S1"}, strategy="distance")
        raise AssertionError("ожидалась ValueError на невзвешенном adj со strategy='distance'")
    except ValueError:
        pass

    print("  синтетика: 4 причины, запрет транзита через клиента, удержание, hops≠distance — OK")


def _demo(path: str) -> None:
    import network  # только для демонстрации на реальных данных — ядро routing.py от network не зависит

    s = geometry.load(path)
    sat_ids = network.satellite_ids(s)
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    clients = sorted(g["id"] for g in s["ground_sites"] if g["role"] == "client")
    grid = list(range(0, int(s["environment"]["horizon_s"]), int(s["environment"]["step_s"])))
    print(f"\n{path}: {len(sat_ids)} спутников, пункты {clients}, шлюзы {sorted(gateways)}, "
          f"{len(grid)} отсчётов")

    for strategy in ("hops", "distance"):
        n_routes = n_none = 0
        reasons: collections.Counter[str] = collections.Counter()
        for t in grid[::11]:  # разреженная выборка по сетке — быстрая, но по всему горизонту
            snap = network.snapshot(s, t)
            adj_plain = network.build_adjacency(snap)
            adj = adj_plain if strategy == "hops" else build_weighted_adjacency(snap)
            for c in clients:
                route = find_route(adj, c, gateways, sat_ids, strategy=strategy)
                if route is None:
                    n_none += 1
                    reason = no_route_reason(s, t, adj_plain, c, gateways, sat_ids)
                    assert reason in REASONS
                    reasons[reason] += 1
                    continue
                n_routes += 1
                assert route[0] == c, route
                assert route[-1] in gateways, route
                assert all(n in sat_ids for n in route[1:-1]), route
                assert all(b in _neighbor_ids(adj_plain, a) for a, b in zip(route, route[1:])), route
        print(f"  strategy={strategy}: {n_routes} маршрутов структурно корректны, "
              f"{n_none} без маршрута {dict(reasons) if reasons else ''}")

    # Удержание предыдущего маршрута: доля отсчётов со сменой пути, с удержанием и без,
    # по каждому пункту (docs/PARAMETERS.md меряет это на C72 и получает 84% → 63%).
    adj_by_t = {t: network.build_adjacency(network.snapshot(s, t)) for t in grid}
    for c in clients:
        prev_hold = prev_free = None
        changes_hold = changes_free = steps_with_route = 0
        for t in grid:
            adj_plain = adj_by_t[t]
            r_free = find_route(adj_plain, c, gateways, sat_ids, strategy="hops")
            r_hold = find_route(adj_plain, c, gateways, sat_ids, strategy="hops", previous=prev_hold)
            if r_free is not None:
                steps_with_route += 1
                if prev_free is not None and r_free != prev_free:
                    changes_free += 1
                if prev_hold is not None and r_hold != prev_hold:
                    changes_hold += 1
            prev_free, prev_hold = r_free, r_hold
        if steps_with_route:
            pct_free = 100 * changes_free / steps_with_route
            pct_hold = 100 * changes_hold / steps_with_route
            print(f"  {c}: смена пути без удержания {changes_free}/{steps_with_route} ({pct_free:.0f}%), "
                  f"с удержанием {changes_hold}/{steps_with_route} ({pct_hold:.0f}%)")
            assert changes_hold <= changes_free, "удержание обязано не увеличивать число перестроений"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    print("routing.py: самопроверка")
    _check_synthetic()

    paths = sys.argv[1:] or [
        str(Path(__file__).resolve().parents[2] / "Данные" / "01_full_constellation.json"),
        str(Path(__file__).resolve().parents[2] / "Данные" / "04_link_range.json"),
    ]
    for p in paths:
        _demo(p)
    print("\nrouting.py: самопроверка пройдена")
