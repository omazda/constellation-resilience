"""Состояние сети группировки в момент времени и перевод координат для фронтенда.

Модуль не переизобретает орбитальную механику: вся геометрия (положения аппаратов,
видимость, критерий ISL) считается `core.geometry.snapshot()` — здесь она только
раскладывается на структуры, удобные маршрутизации и отрисовке:

  - `snapshot(s, t_s)` — то же состояние сети, что отдаёт geometry, но с добавленными
    широтой/долготой/долей радиуса Земли по высоте (для globe.gl) и с рёбрами,
    разложенными на межспутниковые (ISL) и наземные контакты отдельными списками —
    сырой `geometry.snapshot()` отдаёт их одним общим `edges` без различения природы.
  - `build_adjacency(snapshot)` — неориентированный граф связности узлов этого снимка.
  - `split_node_kinds(s)` — множества id спутников и наземных узлов сценария. Нужен
    любому коду маршрутизации: клиентские пункты не ретранслируют, поэтому обход
    графа обязан на каждом шаге отличать спутник от земли, а не только на концах пути.

Точка отсчёта времени — секунды от начала расчёта (`t_s`), не UTC.
"""
from __future__ import annotations

import math
from typing import Any

try:                    # обычный импорт как часть пакета core (from core import network)
    from . import geometry
except ImportError:     # запуск как самостоятельный скрипт: python3 network.py ...
    import geometry


# ─────────────────────────── спутники vs наземные узлы ───────────────────────────

def satellite_ids(s: dict) -> set[str]:
    """ID всех аппаратов группировки сценария — активных и ещё не запущенных."""
    return {sat["id"] for sat in s["design"]["satellites"]}


def ground_site_ids(s: dict) -> set[str]:
    """ID всех наземных узлов сценария — и клиентских пунктов, и шлюзов."""
    return {g["id"] for g in s["ground_sites"]}


def split_node_kinds(s: dict) -> tuple[set[str], set[str]]:
    """(id спутников, id наземных узлов) сценария.

    Нужен всему коду маршрутизации: наземный узел (клиент или шлюз) может быть только
    концом маршрута, не промежуточным звеном. Оба множества статичны для сценария и
    не зависят от t_s — активность/отказ спутника проверяется отдельно, по флагу
    `active` в `snapshot()`, а состав группировки от времени не меняется.
    """
    return satellite_ids(s), ground_site_ids(s)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ───────────────────────────────── снимок сети ────────────────────────────────────

def snapshot(s: dict, t_s: float) -> dict[str, Any]:
    """Состояние сети в момент `t_s`, переведённое для фронтенда и маршрутизации.

    Возвращает:
      t_s
      satellites       — [{id, x_km, y_km, z_km, lat_deg, lon_deg, alt_ratio, active}, ...]
                          x/y/z и active — как в geometry.snapshot() (Земле-фиксированная
                          система координат geometry, поэтому широта/долгота из неё —
                          географические); lat/lon/alt_ratio получены из уже готовых x/y/z:
                            lat = asin(z / r), lon = atan2(y, x), alt_ratio = (r - R) / R
                          globe.gl меряет высоту в радиусах Земли: для 550 км это ≈ 0.086.
      isl_edges         — [{a, b, dist_km}, ...] — только пары спутник-спутник.
      ground_contacts   — [{ground_id, satellite_id, dist_km, elevation_deg}, ...] —
                          только пары наземный узел (клиент или шлюз) — спутник.

    Оба списка рёбер получены разбором `geometry.snapshot(s, t_s)["edges"]` по тому,
    какой из двух id принадлежит множеству наземных узлов — сама geometry их природу
    не различает и отдаёт единым списком.

    Важно: `geometry.snapshot()["elevation_deg"]` содержит углы только по активным
    аппаратам (неактивные в нём отсутствуют вовсе) — здесь это используется только как
    источник elevation_deg для уже отфильтрованных контактов, а не как перечень
    группировки. Полный список аппаратов, включая неактивные, — `satellites` выше и
    `satellite_ids(s)`.
    """
    raw = geometry.snapshot(s, t_s)
    _, ground_ids = split_node_kinds(s)
    elevation = raw["elevation_deg"]

    satellites: list[dict[str, Any]] = []
    for sat in raw["satellites"]:
        x, y, z = sat["x_km"], sat["y_km"], sat["z_km"]
        r = math.sqrt(x * x + y * y + z * z)
        satellites.append({
            "id": sat["id"],
            "x_km": x, "y_km": y, "z_km": z,
            "lat_deg": math.degrees(math.asin(_clamp(z / r, -1.0, 1.0))),
            "lon_deg": math.degrees(math.atan2(y, x)),
            "alt_ratio": (r - geometry.R) / geometry.R,
            "active": sat["active"],
        })

    isl_edges: list[dict[str, Any]] = []
    ground_contacts: list[dict[str, Any]] = []
    for a, b, dist in raw["edges"]:
        a_ground, b_ground = a in ground_ids, b in ground_ids
        if a_ground or b_ground:
            ground_id, sat_id = (a, b) if a_ground else (b, a)
            ground_contacts.append({
                "ground_id": ground_id,
                "satellite_id": sat_id,
                "dist_km": dist,
                "elevation_deg": elevation.get(ground_id, {}).get(sat_id),
            })
        else:
            isl_edges.append({"a": a, "b": b, "dist_km": dist})

    return {
        "t_s": raw["t_s"],
        "satellites": satellites,
        "isl_edges": isl_edges,
        "ground_contacts": ground_contacts,
    }


# ──────────────────────────────── граф связности ──────────────────────────────────

def build_adjacency(snap: dict[str, Any]) -> dict[str, set[str]]:
    """Неориентированный граф контактов снимка: {id узла: {id соседей в этот момент}}.

    Строится из `isl_edges` и `ground_contacts` результата `snapshot()` выше (не из
    сырого `geometry.snapshot()` — там рёбра одним неразличимым списком). Все спутники
    снимка присутствуют как ключи, даже без единого контакта (пустое множество) — так
    вызывающий код отличает «неактивен» от «активен, но сейчас нет связи»; спутники
    без активности всё равно попадают сюда пустым множеством, потому что geometry не
    строит им рёбер. Наземный узел присутствует как ключ, только если у него в этот
    момент есть хотя бы один контакт — для узла без контактов используйте
    `adjacency.get(node_id, set())`.

    Этот граф сам по себе не различает спутник и землю — какие узлы не могут
    ретранслировать трафик, решает вызывающий маршрутизацию код через
    `split_node_kinds()`.
    """
    adjacency: dict[str, set[str]] = {sat["id"]: set() for sat in snap["satellites"]}
    for edge in snap["isl_edges"]:
        a, b = edge["a"], edge["b"]
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    for contact in snap["ground_contacts"]:
        g, sat = contact["ground_id"], contact["satellite_id"]
        adjacency.setdefault(g, set()).add(sat)
        adjacency.setdefault(sat, set()).add(g)
    return adjacency


# ──────────────────────────────────── самопроверка ─────────────────────────────────

def _check_constant_intra_plane_spacing(s: dict, t_s: float = 0.0, tol_rel: float = 1e-6) -> None:
    """В круговой равномерной группировке хорда между соседями по слоту внутри одной
    плоскости постоянна в любой момент времени (см. docs/PARAMETERS.md, §4). Если это
    не так — ошибка в переводе координат этого модуля, а не в geometry.

    Использует только уже вычисленные x/y/z из snapshot() — орбитальные формулы не
    повторяются, дистанция между соседями по кольцу это plain-евклидова норма.
    """
    snap = snapshot(s, t_s)
    pos = {sat["id"]: (sat["x_km"], sat["y_km"], sat["z_km"]) for sat in snap["satellites"]}
    by_plane: dict[str, list[dict]] = {}
    for sat in s["design"]["satellites"]:
        by_plane.setdefault(sat["plane_id"], []).append(sat)
    for plane_id, sats in sorted(by_plane.items()):
        ordered = sorted(sats, key=lambda sat: sat["slot_deg"])
        n = len(ordered)
        if n < 2:
            continue
        chords = [
            math.dist(pos[ordered[k]["id"]], pos[ordered[(k + 1) % n]["id"]])
            for k in range(n)
        ]
        spread = max(chords) - min(chords)
        assert spread < tol_rel * max(chords), (
            f"плоскость {plane_id}: хорды соседей по слоту не постоянны "
            f"(разброс {spread:.6f} км при типичной {chords[0]:.3f} км) — "
            f"ошибка в переводе координат"
        )
        print(f"  plane {plane_id}: {n} sat, хорда соседа по кольцу "
              f"{chords[0]:.2f} км (разброс {spread:.2e} км)")


def _demo(path: str) -> None:
    s = geometry.load(path)
    sat_ids, gnd_ids = split_node_kinds(s)
    print(f"{path}: {len(sat_ids)} спутников, {len(gnd_ids)} наземных узлов "
          f"({sum(1 for g in s['ground_sites'] if g['role'] == 'client')} клиентов, "
          f"{sum(1 for g in s['ground_sites'] if g['role'] == 'gateway')} шлюзов)")

    snap = snapshot(s, 0.0)
    assert {sat["id"] for sat in snap["satellites"]} == sat_ids
    n_active = sum(1 for sat in snap["satellites"] if sat["active"])
    intra = sum(
        1 for e in snap["isl_edges"]
        if next(sat["plane_id"] for sat in s["design"]["satellites"] if sat["id"] == e["a"])
        == next(sat["plane_id"] for sat in s["design"]["satellites"] if sat["id"] == e["b"])
    )
    sat0 = snap["satellites"][0]
    print(f"  t=0: {n_active}/{len(sat_ids)} активны, "
          f"{len(snap['isl_edges'])} ISL-рёбер ({intra} внутриплоскостных), "
          f"{len(snap['ground_contacts'])} наземных контактов")
    print(f"  {sat0['id']}: lat={sat0['lat_deg']:.2f}°, lon={sat0['lon_deg']:.2f}°, "
          f"alt_ratio={sat0['alt_ratio']:.4f}, active={sat0['active']}")

    adjacency = build_adjacency(snap)
    assert set(adjacency) >= sat_ids  # все спутники — ключи, даже без контактов
    for contact in snap["ground_contacts"][:1]:
        assert contact["satellite_id"] in adjacency[contact["ground_id"]]
        assert contact["ground_id"] in adjacency[contact["satellite_id"]]

    _check_constant_intra_plane_spacing(s, 0.0)

    # снимок на другом отсчёте — модуль обязан работать на произвольном t_s, не только t=0
    step = s["environment"]["step_s"]
    snap_mid = snapshot(s, 10 * step)
    assert snap_mid["t_s"] == 10 * step
    assert len(snap_mid["satellites"]) == len(sat_ids)
    print(f"  t={10 * step}: {len(snap_mid['isl_edges'])} ISL-рёбер, "
          f"{len(snap_mid['ground_contacts'])} наземных контактов — ок")


if __name__ == "__main__":
    import sys
    from pathlib import Path

    paths = sys.argv[1:] or [
        str(Path(__file__).resolve().parents[2] / "Данные" / "01_full_constellation.json"),
        str(Path(__file__).resolve().parents[2] / "Данные" / "04_link_range.json"),
    ]
    for p in paths:
        _demo(p)
    print("network.py: самопроверка пройдена")
