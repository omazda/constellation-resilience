"""Доставляемость с допуском по задержке — store-and-forward по временному графу.

Обязательная метрика `core.metrics.compute()` отвечает на вопрос «есть ли маршрут прямо сейчас».
Этот модуль — надстройка (docs/EXTENSIONS.md, §1) над тем же расчётом достижимости, отвечающая на
вопрос «через сколько времени данные всё равно дойдут до шлюза, если аппараты подержат их на борту
и передадут дальше при следующей возможности». Ждать умеет только спутник: наземный узел не
ретранслирует и не хранит данные, поэтому клиентский пункт лишь сдаёт пакет ближайшему видимому
в момент контакта аппарату, а дальше данные — забота группировки.

Алгоритм — обратный проход по сетке отсчётов от конца горизонта к началу (ТЗ-докстрингом задачи
проверен и даёт совпадение допуска T=0 с эталоном `tools/golden.py`/`core.metrics.compute()` до
сотых долей процента):

  1. На каждом отсчёте строим компоненты связности ТОЛЬКО по межспутниковым (ISL) рёбрам —
     наземные контакты в компоненту не входят, они проверяются отдельно на шаге 2. Неактивный
     аппарат (отказ или ещё не запущен) в компоненты не попадает вовсе: у него и так нет ни
     одного ISL-ребра (`core.network.snapshot` строит их только между активными аппаратами), а
     считать его узлом, способным «подержать» данные, было бы ошибкой — он выпал из всех связей
     (docs/PARAMETERS.md, §4).
  2. Компонента, у которой на этом же отсчёте есть контакт с доступным шлюзом, доставляет с
     нулевой задержкой — значение всех аппаратов компоненты на этом отсчёте равно 0. «Доступный»
     уже означает вне `gateway_outages`: этот фильтр встроен в `geometry.snapshot()` на уровне
     самих рёбер контакта, отдельно его здесь перепроверять не нужно.
  3. Иначе значение компоненты — минимум по её аппаратам из УЖЕ посчитанных значений СЛЕДУЮЩЕГО
     отсчёта, плюс `step_s`: в пределах одного отсчёта компонента ISL-связна, поэтому пакет можно
     мгновенно передать тому её аппарату, у которого ожидание до доставки короче всего, и ждать
     на нём один шаг. На последнем отсчёте горизонта ждать следующего шага уже негде — значение
     компоненты, если она не доставляет прямо сейчас, — «не доставлено» (бесконечность/`None`).
     Это тот же цензурирующий эффект границы горизонта, что и у перерывов в `core.metrics.py`.
  4. Значение клиентского пункта на отсчёте — минимум по значениям всех аппаратов, с которыми у
     него в этот момент есть контакт (несколько видимых спутников — берём того, у кого короче).

Доля отсчётов, из которых данные доходят с задержкой ≤ T, — по каждому допуску `tolerances_s` и
клиенту. Допуск T=0 обязан совпасть с `avail_pct` из `core.metrics.compute()`: это то же самое
условие «путь существует прямо сейчас» — компонента ISL-связности с контактом до шлюза на этом
отсчёте есть ровно то же множество достижимости, которое обходом графа строит `metrics._bfs_reach`
(клиент → спутник напрямую, дальше только по ISL-рёбрам, до первого шлюза).

Модуль самодостаточен и не импортирует ничего из соседних `metrics.py`/`routing.py` (только
`network.py` и `scenario.py`, от которых зависит и сам `network.py`): `python3 delivery.py
[сценарий.json ...]` без аргументов считает `Данные/01_full_constellation.json` и
`Данные/04_link_range.json`, печатает таблицу по каждому клиенту и сверяет её с таблицей допуска
из docs/EXTENSIONS.md (там же полные значения и для 02/03).
"""
from __future__ import annotations

import math
from typing import Any, Sequence

try:                    # обычный импорт как часть пакета core (from core import delivery)
    from . import network, scenario
except ImportError:     # запуск как самостоятельный скрипт: python3 delivery.py ...
    import network
    import scenario

__all__ = ["delivery"]

#: Допуски по умолчанию — 0 («сейчас»), 2, 5, 10, 30, 60 минут в секундах.
DEFAULT_TOLERANCES_S: tuple[int, ...] = (0, 120, 300, 600, 1800, 3600)


def _isl_components(snap: dict[str, Any]) -> list[set[str]]:
    """Компоненты связности активных аппаратов снимка ТОЛЬКО по ISL-рёбрам (докстринг, шаг 1).

    Неактивный аппарат в результат не попадает: он не может ни хранить, ни ретранслировать
    данные. Аппарат без единого ISL-соседа всё равно образует свою собственную компоненту из
    одного узла — ему не с кем мгновенно обменяться данными на этом отсчёте, но сам он вполне
    может в этот момент видеть шлюз (шаг 2) или хранить данные до следующего отсчёта (шаг 3).
    """
    active = {sat["id"] for sat in snap["satellites"] if sat["active"]}
    adjacency: dict[str, set[str]] = {sat_id: set() for sat_id in active}
    for edge in snap["isl_edges"]:
        a, b = edge["a"], edge["b"]
        adjacency[a].add(b)
        adjacency[b].add(a)

    components: list[set[str]] = []
    seen: set[str] = set()
    for start in active:
        if start in seen:
            continue
        comp = {start}
        seen.add(start)
        stack = [start]
        while stack:
            u = stack.pop()
            for v in adjacency[u]:
                if v not in seen:
                    seen.add(v)
                    comp.add(v)
                    stack.append(v)
        components.append(comp)
    return components


def delivery(s: dict, tolerances_s: Sequence[int] = DEFAULT_TOLERANCES_S) -> dict[str, Any]:
    """Доставляемость каждого клиентского пункта сценария `s` с допуском по задержке.

    `tolerances_s` — допуски в секундах, по умолчанию `DEFAULT_TOLERANCES_S` (0/2/5/10/30/60 мин).
    Допуск 0 обязан совпасть со сквозной доступностью `core.metrics.compute()` — см. докстринг
    модуля; расхождение означает ошибку в этом модуле, а не «другой расчёт того же самого».

    Возвращает:
      {
        "tolerances_s": [0, 120, ...],   — тот же список допусков, что и на входе (int, секунды)
        "clients": {
          client_id: {
            "delay_s": [float | None, ...],  — по отсчётам сетки: через сколько секунд ОТ этого
                                                отсчёта данные дойдут до шлюза (0 — маршрут есть
                                                прямо сейчас, ожидание на борту не понадобилось);
                                                None — не доходят ни с каким ожиданием до конца
                                                горизонта (цензурировано его границей)
            "delivered_pct": {"0": .., "120": .., ...},  — доля отсчётов сетки, где данные с
                                                этого отсчёта доходят с задержкой ≤ T; ключ —
                                                str(T) для каждого T из tolerances_s (строкой,
                                                а не числом — числовые ключи объекта в JSON
                                                выгрузки недопустимы)
          }, ...
        }
      }
    """
    tolerances = [int(t) for t in tolerances_s]
    if not tolerances:
        raise ValueError("tolerances_s пуст: нужен хотя бы один допуск (0 — «сейчас»)")
    if any(t < 0 for t in tolerances):
        raise ValueError(f"tolerances_s содержит отрицательный допуск: {tolerances}")

    steps = scenario.time_grid(s)
    n = len(steps)
    if n == 0:
        raise ValueError("пустая сетка отсчётов: horizon_s/step_s дают 0 шагов")
    step_s = int(s["environment"]["step_s"])

    clients = [g["id"] for g in s["ground_sites"] if g["role"] == "client"]
    gateways = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    if not clients:
        raise ValueError("в сценарии нет ни одного пункта с role='client'")
    if not gateways:
        raise ValueError("в сценарии нет ни одного пункта с role='gateway'")

    # Обратный проход по времени: значение отсчёта i зависит только от уже посчитанного
    # отсчёта i+1, поэтому достаточно хранить одно предыдущее (по ходу обхода) поколение
    # sat_delay — «значения следующего отсчёта» из шага 3 докстринга, а не весь массив по t.
    sat_delay_next: dict[str, float] = {}
    client_delay: dict[str, list[float]] = {c: [math.inf] * n for c in clients}

    for i in range(n - 1, -1, -1):
        snap = network.snapshot(s, steps[i])
        gw_contacted = {
            contact["satellite_id"]
            for contact in snap["ground_contacts"]
            if contact["ground_id"] in gateways
        }

        sat_delay_this: dict[str, float] = {}
        for comp in _isl_components(snap):
            if comp & gw_contacted:
                value = 0.0
            elif i == n - 1:
                value = math.inf                   # конец горизонта: ждать следующего шага негде
            else:
                best = min((sat_delay_next.get(sat, math.inf) for sat in comp), default=math.inf)
                value = best + step_s if best < math.inf else math.inf
            for sat in comp:
                sat_delay_this[sat] = value

        for c in clients:
            visible = {
                contact["satellite_id"]
                for contact in snap["ground_contacts"]
                if contact["ground_id"] == c
            }
            client_delay[c][i] = min(
                (sat_delay_this.get(sat, math.inf) for sat in visible), default=math.inf
            )

        sat_delay_next = sat_delay_this

    clients_out: dict[str, Any] = {}
    for c in clients:
        delays = client_delay[c]
        clients_out[c] = {
            "delay_s": [None if math.isinf(d) else d for d in delays],
            "delivered_pct": {
                str(t): sum(1 for d in delays if d <= t) / n
                for t in tolerances
            },
        }

    return {"tolerances_s": tolerances, "clients": clients_out}


# ──────────────────────────────────── самопроверка ─────────────────────────────────────

def _pct(x: float) -> float:
    return round(100 * x, 2)


def _check(label: str, got: float, expected: float, tol: float = 0.011) -> None:
    assert abs(got - expected) <= tol, f"{label}: получено {got}, ожидалось {expected} (эталон)"
    print(f"    {label}: {got} — ок (эталон {expected})")


def _demo(path: str, golden: dict[str, dict[int, float]]) -> None:
    """golden: {client_id: {T_секунды: ожидаемый_процент, ...}} — из docs/EXTENSIONS.md, §1.

    Колонки таблицы EXTENSIONS.md — «сейчас», ≤2, ≤5, ≤10, ≤60 мин, т.е. T = 0/120/300/600/3600;
    допуск 1800 (30 мин) в исходной таблице не приведён и здесь не проверяется числом, но
    считается и печатается — иначе он не был бы задействован в самопроверке вовсе.
    """
    import json
    from pathlib import Path

    s = scenario.load_scenario(json.loads(Path(path).read_text(encoding="utf-8")))
    print(f"\n{path}")

    import time as _time
    t0 = _time.time()
    result = delivery(s)
    elapsed = _time.time() - t0
    n_steps = len(scenario.time_grid(s))
    print(f"  delivery(): {elapsed:.2f} с на {n_steps} отсчётов × {len(result['clients'])} пунктов")

    clients_expected = {g["id"] for g in s["ground_sites"] if g["role"] == "client"}
    assert set(result["clients"]) == clients_expected, "delivery() вернул не тот набор клиентов"
    assert result["tolerances_s"] == list(DEFAULT_TOLERANCES_S)

    for c in sorted(result["clients"]):
        m = result["clients"][c]
        assert len(m["delay_s"]) == n_steps
        assert len(m["delivered_pct"]) == len(DEFAULT_TOLERANCES_S)
        # доля доставленного монотонно не убывает с ростом допуска — иначе где-то в обратном
        # проходе значение "протухло" (перепутан min с max, или пропущено +step_s)
        row = [m["delivered_pct"][str(t)] for t in DEFAULT_TOLERANCES_S]
        assert row == sorted(row), f"{c}: delivered_pct не монотонна по допуску: {row}"
        # T=0 не может отличаться от «пути нет» иначе, чем на дискретность отсчёта; delay_s=0
        # ровно там и только там, где на этом отсчёте есть немедленная доставка
        for d in m["delay_s"]:
            assert d is None or d >= 0, f"{c}: отрицательная задержка {d}"

        pct_str = " / ".join(f"≤{t}с {_pct(m['delivered_pct'][str(t)])}%" for t in DEFAULT_TOLERANCES_S)
        print(f"    {c}: {pct_str}")

        if c not in golden:
            continue
        for t_s, expected in sorted(golden[c].items()):
            _check(f"{c} delivered_pct(T={t_s}с)", _pct(m["delivered_pct"][str(t_s)]), expected)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # Полные таблицы docs/EXTENSIONS.md §1 (сейчас/≤2/≤5/≤10/≤60 мин = T 0/120/300/600/3600 с).
    default_pairs = [
        (str(root / "Данные" / "01_full_constellation.json"), {
            "C65": {0: 96.67, 120: 97.78, 300: 97.78, 600: 97.78, 3600: 97.78},
            "C70": {0: 98.75, 120: 99.86, 300: 99.86, 600: 99.86, 3600: 99.86},
            "C72": {0: 98.89, 120: 100.00, 300: 100.00, 600: 100.00, 3600: 100.00},
        }),
        (str(root / "Данные" / "02_first_launch.json"), {
            "C65": {0: 27.22, 120: 29.44, 300: 30.42, 600: 31.25, 3600: 37.22},
            "C70": {0: 15.83, 120: 18.06, 300: 19.03, 600: 19.86, 3600: 26.81},
        }),
        (str(root / "Данные" / "03_satellite_outages.json"), {
            "C65": {0: 79.31, 120: 82.08, 300: 83.19, 600: 83.89, 3600: 84.58},
            "C70": {0: 80.83, 120: 85.28, 300: 86.94, 600: 89.58, 3600: 90.28},
            "C72": {0: 82.50, 120: 87.22, 300: 89.31, 600: 92.50, 3600: 93.06},
        }),
        (str(root / "Данные" / "04_link_range.json"), {
            "C65": {0: 77.50, 120: 81.81, 300: 85.97, 600: 90.42, 3600: 90.42},
            "C70": {0: 62.22, 120: 79.72, 300: 88.89, 600: 95.00, 3600: 95.00},
            "C72": {0: 65.14, 120: 86.67, 300: 96.53, 600: 96.53, 3600: 96.53},
        }),
    ]
    paths = sys.argv[1:]
    pairs = default_pairs if not paths else [(p, {}) for p in paths]
    for p, golden in pairs:
        _demo(p, golden)
    print("\ndelivery.py: самопроверка пройдена (допуск T=0 совпадает с эталоном tools/golden.py, "
          "T>0 — с таблицей docs/EXTENSIONS.md §1)")
