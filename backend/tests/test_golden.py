"""Сверка расчётного ядра `core/` с эталоном на всех четырёх сценариях `Данные/*.json`.

Доступность, видимость хотя бы одного спутника и максимальный перерыв — это достижимость
в графе, а не свойство алгоритма маршрутизации (CLAUDE.md, docs/README.md вывод №1). Поэтому
здесь эти три величины сверяются с эталоном ДВАЖДЫ, независимыми источниками:

  1. со статической таблицей `GOLDEN` — числами из CLAUDE.md («Эталон: python3 tools/golden.py»)
     и `docs/PARAMETERS.md` (видимость 01/04). Это защита от регрессии: числа зафиксированы
     прогонами на данных и не должны тихо поплыть при рефакторинге `core/`.
  2. живым запуском независимой эталонной реализации `tools/golden.py::metrics()` (наивный BFS
     по `geometry.snapshot()`) на тех же файлах `Данные/*.json` — ловит расхождение, даже если
     входные данные когда-нибудь поменяются, а таблицу выше забудут обновить.

Расхождение с любым из двух источников — ошибка модели в `core/`, а не «другой, тоже верный,
подход»: сам эталон объясняет это тем же аргументом (см. докстринг `tools/golden.py`).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "Данные"
BACKEND_DIR = Path(__file__).resolve().parents[1]

# Явная страховка на случай запуска не через `pytest backend/tests` из корня репозитория
# (например, `python3 backend/tests/test_golden.py` или другой rootdir) — модули `core/`
# сами делают то же самое для запуска себя как самостоятельных скриптов.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import scenario, metrics  # noqa: E402


def _load_golden_tool():
    """Загружает `tools/golden.py` как модуль по прямому пути к файлу — независимо от того,
    что добавит в sys.path сам pytest (в `tools/` нет `__init__.py`, полагаться на
    автоматическую вставку rootdir нельзя)."""
    path = ROOT / "tools" / "golden.py"
    spec = importlib.util.spec_from_file_location("cosmo_tools_golden", path)
    assert spec is not None and spec.loader is not None, f"не удалось загрузить {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOLDEN_TOOL = _load_golden_tool()

# Таблица эталона: CLAUDE.md, раздел «Эталон: python3 tools/golden.py», плюс видимость 01/04
# из docs/PARAMETERS.md («Видимость хотя бы одного спутника для 01/04»).
# {файл: {client_id: (vis_pct, avail_pct, max_gap_min)}}
GOLDEN: dict[str, dict[str, tuple[float, float, float]]] = {
    "01_full_constellation.json": {
        "C65": (97.78, 96.67, 8),
        "C70": (99.86, 98.75, 2),
        "C72": (100.00, 98.89, 2),
    },
    "02_first_launch.json": {
        "C65": (38.19, 27.22, 572),
        "C70": (48.75, 15.83, 658),
        "C72": (58.47, 12.64, 796),
    },
    "03_satellite_outages.json": {
        "C65": (84.58, 79.31, 24),
        "C70": (90.28, 80.83, 24),
        "C72": (93.06, 82.50, 20),
    },
    "04_link_range.json": {
        "C65": (97.78, 77.50, 94),
        "C70": (99.86, 62.22, 178),
        "C72": (100.00, 65.14, 4),
    },
}

SCENARIO_FILES = sorted(GOLDEN)


def _load(fname: str) -> dict:
    raw = json.loads((DATA_DIR / fname).read_text(encoding="utf-8"))
    return scenario.load_scenario(raw)


@pytest.fixture(scope="module")
def scenarios() -> dict[str, dict]:
    """Все четыре сценария ТЗ, загруженные и провалидированные `core.scenario.load_scenario`."""
    return {fname: _load(fname) for fname in SCENARIO_FILES}


@pytest.fixture(scope="module")
def computed(scenarios: dict[str, dict]) -> dict[str, dict]:
    """`core.metrics.compute()` по каждому сценарию, стратегия маршрутизации по умолчанию
    'hops' — считается один раз на модуль, отдельные тесты только читают результат."""
    return {fname: metrics.compute(s, strategy="hops") for fname, s in scenarios.items()}


def _pct(fraction: float) -> float:
    return round(100 * fraction, 2)


def _gap_min(seconds: float) -> float:
    return round(seconds / 60, 1)


# ───────────────────────── 1. сверка со статической таблицей эталона ──────────────────────

CASES: list[tuple[str, str, float, float, float]] = [
    (fname, client, vis, avail, gap)
    for fname, clients in GOLDEN.items()
    for client, (vis, avail, gap) in clients.items()
]


@pytest.mark.parametrize(
    "fname, client, exp_vis, exp_avail, exp_gap_min",
    CASES,
    ids=[f"{f}:{c}" for f, c, *_ in CASES],
)
def test_matches_static_golden_table(
    computed: dict[str, dict], fname: str, client: str,
    exp_vis: float, exp_avail: float, exp_gap_min: float,
) -> None:
    m = computed[fname]["clients"][client]
    got_vis, got_avail, got_gap = _pct(m["vis_pct"]), _pct(m["avail_pct"]), _gap_min(m["max_gap_s"])
    assert got_vis == pytest.approx(exp_vis, abs=0.01), (
        f"{fname} {client}: видимость {got_vis}% ≠ эталон CLAUDE.md {exp_vis}%"
    )
    assert got_avail == pytest.approx(exp_avail, abs=0.01), (
        f"{fname} {client}: доступность {got_avail}% ≠ эталон CLAUDE.md {exp_avail}%"
    )
    assert got_gap == pytest.approx(exp_gap_min, abs=0.05), (
        f"{fname} {client}: макс. перерыв {got_gap} мин ≠ эталон CLAUDE.md {exp_gap_min} мин"
    )


# ───────────────────── 2. сверка с независимой реализацией tools/golden.py ─────────────────

@pytest.mark.parametrize("fname", SCENARIO_FILES)
def test_matches_live_golden_tool(
    scenarios: dict[str, dict], computed: dict[str, dict], fname: str,
) -> None:
    """`core.metrics.compute()` сравнивается с независимым обходом `tools/golden.py::metrics()`
    на тех же данных: другая реализация BFS, тот же снимок `geometry.snapshot()`."""
    ref = GOLDEN_TOOL.metrics(scenarios[fname])
    got = computed[fname]["clients"]
    assert set(got) == set(ref), f"{fname}: разный набор клиентов у core и tools/golden.py"
    for client, ref_m in ref.items():
        got_vis, got_avail = _pct(got[client]["vis_pct"]), _pct(got[client]["avail_pct"])
        got_gap = _gap_min(got[client]["max_gap_s"])
        assert got_vis == pytest.approx(ref_m["vis_pct"], abs=0.01), (
            f"{fname} {client}: видимость core={got_vis}% ≠ tools/golden.py={ref_m['vis_pct']}%"
        )
        assert got_avail == pytest.approx(ref_m["avail_pct"], abs=0.01), (
            f"{fname} {client}: доступность core={got_avail}% ≠ tools/golden.py={ref_m['avail_pct']}%"
        )
        assert got_gap == pytest.approx(ref_m["max_gap_min"], abs=0.05), (
            f"{fname} {client}: перерыв core={got_gap} мин ≠ tools/golden.py={ref_m['max_gap_min']} мин"
        )


# ───────────────── 3. маршрутизация не влияет на достижимость (hops vs distance) ───────────

@pytest.mark.parametrize("fname", SCENARIO_FILES)
def test_routing_strategy_does_not_change_reachability(
    scenarios: dict[str, dict], computed: dict[str, dict], fname: str,
) -> None:
    """`strategy='distance'` (Дейкстра по км) обязана дать ТЕ ЖЕ vis/avail/gap, что 'hops' —
    это одна и та же достижимость в графе (CLAUDE.md, docs/PARAMETERS.md §5); различается
    только форма самого маршрута, не эти три показателя."""
    hops = computed[fname]["clients"]
    dist = metrics.compute(scenarios[fname], strategy="distance")["clients"]
    for client in hops:
        assert dist[client]["vis_pct"] == pytest.approx(hops[client]["vis_pct"], abs=1e-9), (
            f"{fname} {client}: vis_pct отличается между стратегиями маршрутизации"
        )
        assert dist[client]["avail_pct"] == pytest.approx(hops[client]["avail_pct"], abs=1e-9), (
            f"{fname} {client}: avail_pct отличается между стратегиями маршрутизации"
        )
        assert dist[client]["max_gap_s"] == hops[client]["max_gap_s"], (
            f"{fname} {client}: max_gap_s отличается между стратегиями маршрутизации"
        )


# ─────────────── 4. выгрузка результата проходит проверку `tools/golden.py --result` ────────

@pytest.mark.parametrize("fname", SCENARIO_FILES)
def test_result_export_passes_golden_check(
    scenarios: dict[str, dict], computed: dict[str, dict], fname: str, tmp_path: Path,
) -> None:
    """Пакет в формате `cosmo-A-result-1.0`, собранный из `compute()`, обязан проходить
    `tools/golden.py::check_result` без единой ошибки: формат, полнота (ровно по одной записи
    на каждую пару (t_s, client_id)), физическая допустимость каждого маршрута (клиент не
    ретранслирует, каждый узел/ребро пути существует на своём отсчёте) и совпадение
    посчитанной по выгрузке доступности с эталоном."""
    result: dict[str, Any] = {
        "schema_version": "cosmo-A-result-1.0",
        "effective_scenario": scenario.export_scenario(scenarios[fname]),
        "routes": computed[fname]["routes"],
    }
    out = tmp_path / "result.json"
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    problems = GOLDEN_TOOL.check_result(str(out))
    assert not problems, f"{fname}: выгрузка не прошла tools/golden.py --result:\n" + "\n".join(problems)


# ───────────────────── 5. форма routes: без транзита через клиента, минимум 2 перехода ──────

@pytest.mark.parametrize("fname", SCENARIO_FILES)
def test_routes_never_relay_through_ground_site(
    scenarios: dict[str, dict], computed: dict[str, dict], fname: str,
) -> None:
    """Ни один маршрут не проходит через посторонний наземный узел (другой клиент или шлюз как
    транзит) — наивный обход графа как раз на этом завышает доступность (docs/PARAMETERS.md §4,
    до +11 п.п. на сценарии 04). Плюс: путь непуст ⇔ есть маршрут, длина ≥ 2 переходов
    (клиент→…→шлюз — минимум одно наземное ребро с каждой стороны)."""
    s = scenarios[fname]
    ground_ids = {g["id"] for g in s["ground_sites"]}
    gateway_ids = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    n_checked = 0
    for rec in computed[fname]["routes"]:
        path = rec["path"]
        if not path:
            continue
        n_checked += 1
        assert path[0] == rec["client_id"], f"{fname} t_s={rec['t_s']}: путь не начинается в клиенте: {path}"
        assert path[-1] in gateway_ids, f"{fname} t_s={rec['t_s']}: путь не заканчивается в шлюзе: {path}"
        assert len(path) >= 3, f"{fname} t_s={rec['t_s']}: переходов меньше 2 (путь {path})"
        for node in path[1:-1]:
            assert node not in ground_ids, (
                f"{fname} t_s={rec['t_s']} client={rec['client_id']}: наземный узел {node!r} "
                f"как транзит в маршруте {path} — клиентские пункты не ретранслируют"
            )
    assert n_checked > 0, f"{fname}: ни одного непустого маршрута — тест ничего не проверил"


# ───────────────────────────── 6. round-trip экспорта сценария ─────────────────────────────

@pytest.mark.parametrize("fname", SCENARIO_FILES)
def test_scenario_export_roundtrip(fname: str) -> None:
    """`export_scenario(load_scenario(raw)) == raw` для валидного входа: выгрузка изменённого
    сценария в `cosmo-A-1.0` и его повторная загрузка обязаны быть точными (docs/PARAMETERS.md
    §6) — иначе UI, сохранивший вариант, при повторной загрузке получит не то, что сохранял."""
    raw = json.loads((DATA_DIR / fname).read_text(encoding="utf-8"))
    loaded = scenario.load_scenario(raw)
    assert scenario.export_scenario(loaded) == raw


def test_scenario_export_roundtrip_survives_patch() -> None:
    """Тот же round-trip, но после `patch_scenario` (RAAN/фаза плоскости, смена очереди
    запуска, добавление отказа) — ровно тот сценарий обязателен для приёмки: сохранить вариант,
    выгрузить, снова загрузить и получить идентичный сценарий."""
    raw = json.loads((DATA_DIR / "01_full_constellation.json").read_text(encoding="utf-8"))
    s = scenario.load_scenario(raw)
    patched = scenario.patch_scenario(s, {
        "launch_stage": 2,
        "planes": {"P1": {"raan_deg": 45.0}},
        "add_failures": [{"satellite_id": "S01", "start_s": 0, "end_s": 3600}],
    })
    exported = scenario.export_scenario(patched)
    reloaded = scenario.load_scenario(exported)
    assert scenario.export_scenario(reloaded) == exported


def test_scenario_hash_ignores_meta_but_reacts_to_design() -> None:
    """`scenario_hash` не должен меняться от косметической правки `meta` (название/id варианта),
    но обязан меняться от правки, влияющей на расчёт — иначе кеш результата отдаст старые числа
    для нового варианта (docs/PROTOCOL.md, кеш по хешу сценария)."""
    raw = json.loads((DATA_DIR / "01_full_constellation.json").read_text(encoding="utf-8"))
    s = scenario.load_scenario(raw)
    h1 = scenario.scenario_hash(s)

    s_renamed = json.loads(json.dumps(s))
    s_renamed["meta"] = {"id": "same-scenario-renamed", "title": "Косметическая правка названия"}
    assert scenario.scenario_hash(s_renamed) == h1, "правка meta не должна менять хеш"

    patched = scenario.patch_scenario(s, {"launch_stage": 1})
    assert scenario.scenario_hash(patched) != h1, "смена launch_stage обязана менять хеш"


# ─────────────────────────────── 7. валидация входа: понятные ошибки ───────────────────────

def test_validation_error_names_the_offending_field() -> None:
    """Ошибка валидации называет проблемное поле, а не роняет stack trace (ТЗ, CLAUDE.md
    «Валидация входа»). Пример: `horizon_s`, не кратный `step_s`."""
    raw = json.loads((DATA_DIR / "01_full_constellation.json").read_text(encoding="utf-8"))
    broken = json.loads(json.dumps(raw))
    broken["environment"]["horizon_s"] = broken["environment"]["step_s"] * 3 + 1
    with pytest.raises(scenario.ScenarioError) as exc_info:
        scenario.load_scenario(broken)
    assert exc_info.value.field == "environment.horizon_s"


def test_validation_rejects_route_relay_reference_gracefully() -> None:
    """Ссылка на несуществующую плоскость называет конкретный индекс/поле спутника, а не
    падает `KeyError`."""
    raw = json.loads((DATA_DIR / "01_full_constellation.json").read_text(encoding="utf-8"))
    broken = json.loads(json.dumps(raw))
    broken["design"]["satellites"][0]["plane_id"] = "P_NOT_EXISTING"
    with pytest.raises(scenario.ScenarioError) as exc_info:
        scenario.load_scenario(broken)
    assert exc_info.value.field == "design.satellites[0].plane_id"
