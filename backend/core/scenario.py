"""Загрузка, валидация, правки и экспорт сценария `cosmo-A-1.0`.

Правила расчёта задаёт `geometry.py` (не трогаем). Этот модуль отвечает за то, что происходит
*до* геометрии: разбор входного JSON, понятные ошибки валидации с именем проблемного поля/объекта
(этого требует ТЗ — жюри проверяет сообщением, а не трейсбеком), частичные правки конфигурации
из UI (`launch_stage`, RAAN/фаза плоскости, периоды недоступности) и обратный экспорт.

Проверки границ здесь ровно те же, что в `geometry.validate()` — расхождение с ним означает,
что сценарий, прошедший нашу валидацию, не пройдёт эталонную (и наоборот). Мы лишь заменяем
общие `ValueError` на `ScenarioError(field, message)` с точным путём до поля/объекта и обрабатываем
отсутствующие ключи как ошибку валидации, а не `KeyError`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

SCHEMA_VERSION = "cosmo-A-1.0"

__all__ = [
    "ScenarioError",
    "load_scenario",
    "time_grid",
    "patch_scenario",
    "export_scenario",
    "scenario_hash",
    "active_ids",
    "gateway_online",
]


class ScenarioError(Exception):
    """Ошибка валидации/правки сценария с именем проблемного поля или объекта.

    `field` — путь до места ошибки в духе `design.planes[1].raan_deg` или `failures[3]`,
    его показывают пользователю рядом с сообщением вместо трейсбека.
    """

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def _finite(x: Any) -> bool:
    """Конечное число, не bool (`True`/`False` — валидный `int` в Python, но не число сценария)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _require(cond: bool, field: str, message: str) -> None:
    if not cond:
        raise ScenarioError(field, message)


def _get(obj: dict, key: str, field: str) -> Any:
    """Достаёт `obj[key]`, поднимая ScenarioError с именем поля вместо KeyError."""
    if key not in obj:
        raise ScenarioError(field, "поле отсутствует")
    return obj[key]


def _validate(s: dict) -> None:
    """Полная проверка сценария. Границы — как в `geometry.validate()`, только с именованными
    ошибками и дополнительными требованиями «Описания данных» (конечные числа, уникальные ID,
    разрешимые ссылки, `horizon_s % step_s == 0`, интервалы недоступности внутри горизонта
    и положительной длины)."""
    _require(isinstance(s, dict), "$", "сценарий должен быть JSON-объектом")
    if s.get("schema_version") != SCHEMA_VERSION:
        raise ScenarioError(
            "schema_version",
            f"ожидается {SCHEMA_VERSION!r}, получено {s.get('schema_version')!r}",
        )

    e = _get(s, "environment", "environment")
    _require(isinstance(e, dict), "environment", "должен быть объектом")
    d = _get(s, "design", "design")
    _require(isinstance(d, dict), "design", "должен быть объектом")

    for key in (
        "altitude_km", "inclination_deg", "earth_angle0_deg", "horizon_s",
        "step_s", "min_elevation_deg", "isl_range_km", "target_availability",
    ):
        val = _get(e, key, f"environment.{key}")
        _require(_finite(val), f"environment.{key}", "должно быть конечным числом")

    _require(200 <= e["altitude_km"] <= 1200, "environment.altitude_km",
              "должно быть в диапазоне [200, 1200] км")
    _require(0 < e["inclination_deg"] <= 180, "environment.inclination_deg",
              "должно быть в диапазоне (0, 180] град.")
    _require(_is_int(e["step_s"]), "environment.step_s", "должно быть целым числом секунд")
    _require(_is_int(e["horizon_s"]), "environment.horizon_s", "должно быть целым числом секунд")
    _require(e["step_s"] > 0, "environment.step_s", "должно быть положительным")
    _require(e["step_s"] <= e["horizon_s"], "environment.step_s", "не может превышать horizon_s")
    _require(e["horizon_s"] <= 172800, "environment.horizon_s",
              "не может превышать 172800 с (48 часов, ограничение geometry.validate)")
    _require(e["horizon_s"] % e["step_s"] == 0, "environment.horizon_s",
              "должно быть кратно step_s")
    _require(0 <= e["min_elevation_deg"] < 90, "environment.min_elevation_deg",
              "должно быть в диапазоне [0, 90) град.")
    _require(0 < e["isl_range_km"] <= 10000, "environment.isl_range_km",
              "должно быть в диапазоне (0, 10000] км")
    _require(0 <= e["target_availability"] <= 1, "environment.target_availability",
              "должно быть в диапазоне [0, 1]")

    planes_raw = _get(d, "planes", "design.planes")
    _require(isinstance(planes_raw, list) and len(planes_raw) > 0, "design.planes",
              "должен быть непустым списком")
    planes: dict[str, dict] = {}
    for i, p in enumerate(planes_raw):
        fb = f"design.planes[{i}]"
        _require(isinstance(p, dict), fb, "должен быть объектом")
        pid = _get(p, "id", f"{fb}.id")
        _require(isinstance(pid, str) and pid != "", f"{fb}.id", "должен быть непустой строкой")
        _require(pid not in planes, f"{fb}.id", f"повторяющийся ID плоскости {pid!r}")
        for key in ("raan_deg", "phase_deg"):
            val = _get(p, key, f"{fb}.{key}")
            _require(_finite(val), f"{fb}.{key}", "должно быть конечным числом")
            _require(0 <= val < 360, f"{fb}.{key}", "должно быть в диапазоне [0, 360) — 360 недопустимо")
        planes[pid] = p

    sats_raw = _get(d, "satellites", "design.satellites")
    _require(isinstance(sats_raw, list) and len(sats_raw) > 0, "design.satellites",
              "должен быть непустым списком")
    sat_ids: set[str] = set()
    for i, sat in enumerate(sats_raw):
        fb = f"design.satellites[{i}]"
        _require(isinstance(sat, dict), fb, "должен быть объектом")
        sid = _get(sat, "id", f"{fb}.id")
        _require(isinstance(sid, str) and sid != "", f"{fb}.id", "должен быть непустой строкой")
        _require(sid not in sat_ids, f"{fb}.id", f"повторяющийся ID аппарата {sid!r}")
        sat_ids.add(sid)
        pid = _get(sat, "plane_id", f"{fb}.plane_id")
        _require(pid in planes, f"{fb}.plane_id", f"ссылка на несуществующую плоскость {pid!r}")
        batch = _get(sat, "launch_batch", f"{fb}.launch_batch")
        _require(_is_int(batch) and batch in (1, 2, 3), f"{fb}.launch_batch",
                  "должно быть целым числом 1, 2 или 3")
        slot = _get(sat, "slot_deg", f"{fb}.slot_deg")
        _require(_finite(slot), f"{fb}.slot_deg", "должно быть конечным числом")

    launch_stage = _get(d, "launch_stage", "design.launch_stage")
    _require(_is_int(launch_stage) and launch_stage in (1, 2, 3), "design.launch_stage",
              "должно быть целым числом 1, 2 или 3")

    ground = _get(s, "ground_sites", "ground_sites")
    _require(isinstance(ground, list) and len(ground) > 0, "ground_sites",
              "должен быть непустым списком")
    ground_ids: set[str] = set()
    gateway_ids: set[str] = set()
    has_client = has_gateway = False
    for i, g in enumerate(ground):
        fb = f"ground_sites[{i}]"
        _require(isinstance(g, dict), fb, "должен быть объектом")
        gid = _get(g, "id", f"{fb}.id")
        _require(isinstance(gid, str) and gid != "", f"{fb}.id", "должен быть непустой строкой")
        _require(gid not in ground_ids, f"{fb}.id", f"повторяющийся ID наземного пункта {gid!r}")
        _require(gid not in sat_ids, f"{fb}.id", f"ID {gid!r} совпадает с ID аппарата")
        ground_ids.add(gid)
        role = _get(g, "role", f"{fb}.role")
        _require(role in ("client", "gateway"), f"{fb}.role", "должно быть 'client' или 'gateway'")
        if role == "client":
            has_client = True
        else:
            has_gateway = True
            gateway_ids.add(gid)
        lat = _get(g, "lat_deg", f"{fb}.lat_deg")
        lon = _get(g, "lon_deg", f"{fb}.lon_deg")
        _require(_finite(lat) and -90 <= lat <= 90, f"{fb}.lat_deg",
                  "должно быть конечным числом в диапазоне [-90, 90]")
        _require(_finite(lon) and -180 <= lon <= 180, f"{fb}.lon_deg",
                  "должно быть конечным числом в диапазоне [-180, 180]")
    _require(has_client, "ground_sites", "должен быть хотя бы один пункт с role='client'")
    _require(has_gateway, "ground_sites", "должен быть хотя бы один пункт с role='gateway' (шлюзов может быть несколько)")

    def _check_outages(list_field: str, id_key: str, valid_ids: set[str], id_label: str) -> None:
        items = _get(s, list_field, list_field)
        _require(isinstance(items, list), list_field, "должен быть списком")
        for i, f in enumerate(items):
            fb = f"{list_field}[{i}]"
            _require(isinstance(f, dict), fb, "должен быть объектом")
            ref = _get(f, id_key, f"{fb}.{id_key}")
            _require(ref in valid_ids, f"{fb}.{id_key}", f"ссылка на несуществующий {id_label} {ref!r}")
            start = _get(f, "start_s", f"{fb}.start_s")
            end = _get(f, "end_s", f"{fb}.end_s")
            _require(_finite(start), f"{fb}.start_s", "должно быть конечным числом")
            _require(_finite(end), f"{fb}.end_s", "должно быть конечным числом")
            _require(0 <= start < end <= e["horizon_s"], fb,
                      f"интервал [{start}, {end}) должен быть внутри горизонта [0, {e['horizon_s']}] "
                      "и иметь положительную длину (start_s < end_s)")

    _check_outages("failures", "satellite_id", sat_ids, "аппарат")
    _check_outages("gateway_outages", "gateway_id", gateway_ids, "шлюз")


def load_scenario(raw: dict) -> dict:
    """Проверяет сырой JSON по схеме `cosmo-A-1.0` и возвращает нормализованную копию.

    Поднимает `ScenarioError(field, message)` с именем проблемного поля/объекта на первой же
    найденной проблеме (ошибки не накапливаются — так сообщение остаётся однозначным). Значения
    не преобразуются и не округляются: `export_scenario(load_scenario(raw))` равен `raw`.
    """
    if not isinstance(raw, dict):
        raise ScenarioError("$", "сценарий должен быть JSON-объектом")
    s = copy.deepcopy(raw)
    _validate(s)
    return s


def time_grid(s: dict) -> list[int]:
    """Сетка отсчётов `0, step_s, ..., horizon_s - step_s`. Правый конец НЕ включается."""
    e = s["environment"]
    return list(range(0, int(e["horizon_s"]), int(e["step_s"])))


def active_ids(s: dict, t_s: float) -> set[str]:
    """ID активных в момент `t_s` аппаратов: `launch_batch <= launch_stage` и момент вне
    `failures` этого аппарата. Интервалы отказа полуоткрытые — `[start_s, end_s)`."""
    d = s["design"]
    stage = d["launch_stage"]
    failed = {f["satellite_id"] for f in s["failures"] if f["start_s"] <= t_s < f["end_s"]}
    return {sat["id"] for sat in d["satellites"] if sat["launch_batch"] <= stage and sat["id"] not in failed}


def gateway_online(s: dict, gw_id: str, t_s: float) -> bool:
    """True, если шлюз `gw_id` не попал в момент `t_s` в свой `gateway_outages` (`[start_s, end_s)`)."""
    return not any(
        o["gateway_id"] == gw_id and o["start_s"] <= t_s < o["end_s"]
        for o in s["gateway_outages"]
    )


def patch_scenario(s: dict, patch: dict) -> dict:
    """Применяет частичную правку конфигурации к сценарию и возвращает НОВЫЙ сценарий
    (исходный `s` не меняется). Результат всегда проходит ту же валидацию, что и загрузка —
    иначе `ScenarioError`.

    Формат `patch` (каждый ключ опционален):

        {
          "launch_stage": 2,
          "planes": {"P1": {"raan_deg": 30.0}, "P2": {"raan_deg": 90.0, "phase_deg": 10.0}},
          "add_failures": [{"satellite_id": "S01", "start_s": 0, "end_s": 3600}],
          "remove_failures": [{"satellite_id": "S01", "start_s": 0, "end_s": 3600}]
        }

    `planes` — правки по ID плоскости, частичные (можно передать только `raan_deg` или только
    `phase_deg`). `remove_failures`/`add_failures` сопоставляют объект отказа целиком
    (`satellite_id`+`start_s`+`end_s`), т.к. у `failures` нет собственного ID.
    """
    _require(isinstance(patch, dict), "patch", "должен быть объектом")
    out = copy.deepcopy(s)
    d = out["design"]

    if "launch_stage" in patch:
        stage = patch["launch_stage"]
        _require(_is_int(stage) and stage in (1, 2, 3), "patch.launch_stage",
                  "должно быть целым числом 1, 2 или 3")
        d["launch_stage"] = stage

    if "planes" in patch:
        planes_patch = patch["planes"]
        _require(isinstance(planes_patch, dict), "patch.planes",
                  "должен быть объектом вида {plane_id: {raan_deg?, phase_deg?}}")
        plane_by_id = {p["id"]: p for p in d["planes"]}
        for pid, values in planes_patch.items():
            fb = f"patch.planes.{pid}"
            _require(pid in plane_by_id, fb, f"плоскость {pid!r} не найдена в сценарии")
            _require(isinstance(values, dict), fb, "должен быть объектом")
            for key in ("raan_deg", "phase_deg"):
                if key not in values:
                    continue
                val = values[key]
                _require(_finite(val), f"{fb}.{key}", "должно быть конечным числом")
                _require(0 <= val < 360, f"{fb}.{key}",
                          "должно быть в диапазоне [0, 360) — 360 недопустимо")
                plane_by_id[pid][key] = float(val)

    if "remove_failures" in patch:
        to_remove = patch["remove_failures"]
        _require(isinstance(to_remove, list), "patch.remove_failures", "должен быть списком")
        remaining = list(out["failures"])
        for i, item in enumerate(to_remove):
            fb = f"patch.remove_failures[{i}]"
            _require(isinstance(item, dict), fb, "должен быть объектом")
            match = next(
                (f for f in remaining
                 if f.get("satellite_id") == item.get("satellite_id")
                 and f.get("start_s") == item.get("start_s")
                 and f.get("end_s") == item.get("end_s")),
                None,
            )
            _require(match is not None, fb, "такого периода недоступности нет в сценарии")
            remaining.remove(match)
        out["failures"] = remaining

    if "add_failures" in patch:
        to_add = patch["add_failures"]
        _require(isinstance(to_add, list), "patch.add_failures", "должен быть списком")
        for i, item in enumerate(to_add):
            fb = f"patch.add_failures[{i}]"
            _require(isinstance(item, dict), fb, "должен быть объектом")
            for key in ("satellite_id", "start_s", "end_s"):
                _require(key in item, f"{fb}.{key}", "поле отсутствует")
            out["failures"].append({
                "satellite_id": item["satellite_id"],
                "start_s": item["start_s"],
                "end_s": item["end_s"],
            })

    _validate(out)
    return out


def export_scenario(s: dict) -> dict:
    """Возвращает сценарий в формате `cosmo-A-1.0`, готовый к сохранению и повторной загрузке.

    Ничего не меняет и не пересчитывает — только копирует и на всякий случай перепроверяет
    инвариант (сценарий внутри `core/` всегда валиден). round-trip точный: для валидного `raw`
    `export_scenario(load_scenario(raw)) == raw`.
    """
    out = copy.deepcopy(s)
    _validate(out)
    return out


def scenario_hash(s: dict) -> str:
    """Устойчивый хеш сценария для кеширования результата расчёта по хешу (см. `docs/PROTOCOL.md`).

    В хеш идёт всё, что влияет на геометрию и достижимость: `environment`, `design`,
    `ground_sites`, `failures`, `gateway_outages`. `meta` (id/название сценария) сознательно
    исключён — косметическая правка названия не должна сбрасывать кеш расчёта.
    """
    payload = {
        "schema_version": s.get("schema_version"),
        "environment": s.get("environment"),
        "design": s.get("design"),
        "ground_sites": s.get("ground_sites"),
        "failures": s.get("failures"),
        "gateway_outages": s.get("gateway_outages"),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent.parent / "Данные" / "01_full_constellation.json"
    )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    scenario = load_scenario(raw)
    print(f"{path}: загружен, {len(scenario['design']['satellites'])} аппаратов, "
          f"{len(scenario['ground_sites'])} наземных пунктов, {len(time_grid(scenario))} отсчётов")
    print("scenario_hash:", scenario_hash(scenario))
    assert export_scenario(scenario) == raw, "round-trip export_scenario != исходный JSON"
    print("round-trip export_scenario: OK")

    patched = patch_scenario(scenario, {"launch_stage": 1})
    print("активных аппаратов при launch_stage=1:", len(active_ids(patched, 0)),
          "(было при исходном launch_stage:", len(active_ids(scenario, 0)), ")")
