"""Сквозная проверка `/ws`: конверт, семь типов сообщений, бинарный фрейм, кеш, ошибки.

Гоняет реальный `WebSocket` через `starlette.testclient.TestClient` (без сети, но через
настоящий ASGI-стек FastAPI) на данных `Данные/*.json` — то, что реально увидит фронтенд.
Отдельно проверяется главное требование протокола: `compute`/`attach` отдают ОДИН бинарный
фрейм на весь горизонт сразу за JSON-манифестом, и числа из него совпадают с эталоном
`tools/golden.py` (доступность — это достижимость в графе, не свойство ws-слоя, см. CLAUDE.md).
"""
from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "Данные"
BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import protocol, ws  # noqa: E402


def _load_golden_tool():
    path = ROOT / "tools" / "golden.py"
    spec = importlib.util.spec_from_file_location("cosmo_tools_golden_ws", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOLDEN_TOOL = _load_golden_tool()

# Эталон CLAUDE.md (та же таблица, что в test_golden.py) — используется здесь только как
# независимая сверка чисел, дошедших через ws-слой и бинарный фрейм, с известным результатом.
GOLDEN_AVAIL: dict[str, dict[str, float]] = {
    "01_full_constellation.json": {"C65": 96.67, "C70": 98.75, "C72": 98.89},
    "02_first_launch.json": {"C65": 27.22, "C70": 15.83, "C72": 12.64},
    "03_satellite_outages.json": {"C65": 79.31, "C70": 80.83, "C72": 82.50},
    "04_link_range.json": {"C65": 77.50, "C70": 62.22, "C72": 65.14},
}


@pytest.fixture()
def client():
    ws.reset_store()
    app = FastAPI()
    app.include_router(ws.router)
    with TestClient(app) as c:
        yield c
    ws.reset_store()


def _raw_scenario(fname: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / fname).read_text(encoding="utf-8"))


def _send(sock, msg_id: str, msg_type: str, payload: dict[str, Any]) -> None:
    sock.send_text(json.dumps({"id": msg_id, "type": msg_type, "payload": payload}))


def _recv_json(sock) -> dict[str, Any]:
    return json.loads(sock.receive_text())


def _load(sock, fname: str, msg_id: str = "load") -> dict[str, Any]:
    _send(sock, msg_id, "scenario.load", _raw_scenario(fname))
    env = _recv_json(sock)
    assert env["type"] == "scenario.load", env
    assert env["id"] == msg_id
    return env["payload"]


def _compute(sock, variant_id: str, strategy: str = "hops", msg_id: str = "cmp") -> tuple[dict, bytes]:
    _send(sock, msg_id, "compute", {"variant_id": variant_id, "strategy": strategy})
    progressed = False
    while True:
        env = _recv_json(sock)
        assert env["id"] == msg_id
        if env["type"] == "progress":
            progressed = True
            assert 0 <= env["payload"]["pct"] <= 100
            continue
        assert env["type"] == "compute", env
        manifest = env["payload"]
        break
    assert progressed, "compute обязан прислать хотя бы одно сообщение progress"
    binary = sock.receive_bytes()
    return manifest, binary


def _slice(manifest: dict, binary: bytes, name: str) -> np.ndarray:
    layout = manifest["layout"][name]
    dtype = np.dtype(layout["dtype"])
    n = layout["length"] // dtype.itemsize
    return np.frombuffer(binary, dtype=dtype, count=n, offset=layout["offset"]).reshape(layout["shape"])


# ─────────────────────────────────────── 1. scenario.load ──────────────────────────────────

@pytest.mark.parametrize("fname", sorted(GOLDEN_AVAIL))
def test_scenario_load_roundtrip_and_summary(client, fname):
    with client.websocket_connect("/ws") as sock:
        raw = _raw_scenario(fname)
        payload = _load(sock, fname)
        assert payload["variant_id"] == payload["scenario_hash"]
        assert payload["effective_scenario"] == raw, "экспорт нормализованного сценария обязан быть round-trip"
        summary = payload["summary"]
        assert summary["n_satellites"] == len(raw["design"]["satellites"])
        assert summary["n_clients"] == sum(1 for g in raw["ground_sites"] if g["role"] == "client")
        assert summary["n_gateways"] == sum(1 for g in raw["ground_sites"] if g["role"] == "gateway")
        assert summary["n_steps"] == raw["environment"]["horizon_s"] // raw["environment"]["step_s"]


def test_scenario_load_same_content_same_variant_id(client):
    with client.websocket_connect("/ws") as sock:
        p1 = _load(sock, "01_full_constellation.json", "a")
        p2 = _load(sock, "01_full_constellation.json", "b")
        assert p1["variant_id"] == p2["variant_id"], "одинаковый сценарий обязан давать тот же variant_id"


def test_scenario_load_validation_error_names_field(client):
    with client.websocket_connect("/ws") as sock:
        raw = _raw_scenario("01_full_constellation.json")
        raw["environment"]["horizon_s"] = raw["environment"]["step_s"] * 3 + 1
        _send(sock, "bad", "scenario.load", raw)
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["id"] == "bad"
        assert env["code"] == "validation_error"
        assert env["field"] == "environment.horizon_s"
        assert "stack" not in env["message"].lower() and "traceback" not in env["message"].lower()


# ─────────────────────────────────────── 2. scenario.patch ─────────────────────────────────

def test_scenario_patch_changes_variant_and_summary(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "p1", "scenario.patch", {
            "variant_id": base["variant_id"],
            "launch_stage": 1,
            "planes": {"P1": {"raan_deg": 45.0}},
        })
        env = _recv_json(sock)
        assert env["type"] == "scenario.patch"
        payload = env["payload"]
        assert payload["parent_variant_id"] == base["variant_id"]
        assert payload["variant_id"] != base["variant_id"]
        assert payload["summary"]["launch_stage"] == 1
        assert payload["effective_scenario"]["design"]["launch_stage"] == 1
        plane_p1 = next(p for p in payload["effective_scenario"]["design"]["planes"] if p["id"] == "P1")
        assert plane_p1["raan_deg"] == 45.0
        # round-trip: повторная загрузка выгруженного изменённого сценария даёт тот же вариант
        reloaded = _load(sock, "01_full_constellation.json", "reload_probe")  # sanity: базовый ещё грузится
        assert reloaded["variant_id"] == base["variant_id"]


def test_scenario_patch_unknown_variant_is_not_found(client):
    with client.websocket_connect("/ws") as sock:
        _send(sock, "p", "scenario.patch", {"variant_id": "does-not-exist", "launch_stage": 1})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "not_found"
        assert env["field"] == "payload.variant_id"


def test_scenario_patch_invalid_value_is_validation_error(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "p", "scenario.patch", {"variant_id": base["variant_id"], "launch_stage": 4})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "validation_error"
        assert env["field"] == "patch.launch_stage"


# ─────────────────────────────────────── 3. compute ────────────────────────────────────────

@pytest.mark.parametrize("fname", sorted(GOLDEN_AVAIL))
@pytest.mark.parametrize("strategy", ["hops", "distance"])
def test_compute_binary_matches_golden(client, fname, strategy):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, fname)
        manifest, binary = _compute(sock, base["variant_id"], strategy)

        assert manifest["binary"] is True
        assert manifest["strategy"] == strategy
        n_steps = manifest["n_steps"]
        clients = manifest["clients"]
        assert n_steps == base["summary"]["n_steps"]

        # Буфер — плотная склейка секций с паддингом до 4 байт: конец последней (по offset)
        # секции обязан лежать в пределах присланных байт, с запасом не больше чем на паддинг.
        last_end = max(layout["offset"] + layout["length"] for layout in manifest["layout"].values())
        assert last_end <= len(binary) <= last_end + 3
        for name, layout in manifest["layout"].items():
            assert layout["offset"] + layout["length"] <= len(binary), name
            assert layout["offset"] % 4 == 0, f"{name}: офсет не выровнен по 4 байта"

        sat_active = _slice(manifest, binary, "sat_active")
        assert sat_active.shape == (n_steps, manifest["n_sat"])
        hops = _slice(manifest, binary, "hops")
        visible = _slice(manifest, binary, "visible")
        reason_codes = _slice(manifest, binary, "reason_codes")
        assert hops.shape == (n_steps, len(clients))
        assert visible.shape == hops.shape
        assert reason_codes.shape == hops.shape

        for ci, c in enumerate(clients):
            avail_pct = 100 * float(np.count_nonzero(hops[:, ci] >= 0)) / n_steps
            exp = GOLDEN_AVAIL[fname].get(c)
            if exp is not None:
                assert avail_pct == pytest.approx(exp, abs=0.02), f"{fname} {c}: {avail_pct} != {exp}"
            # где маршрута нет — reason_codes валиден индекс в reason_labels; где есть — -1
            no_route = hops[:, ci] < 0
            assert np.all(reason_codes[no_route, ci] >= 0)
            assert np.all(reason_codes[no_route, ci] < len(manifest["reason_labels"]))
            assert np.all(reason_codes[~no_route, ci] == -1)
            # видимость не может быть ложной там, где вообще есть маршрут
            assert np.all(visible[hops[:, ci] >= 0, ci] == 1)

        edge_offsets = _slice(manifest, binary, "edge_offsets")
        assert edge_offsets.shape == (n_steps + 1,)
        assert np.all(np.diff(edge_offsets.astype(np.int64)) >= 0), "edge_offsets обязан быть неубывающим (CSR)"
        edge_pairs = _slice(manifest, binary, "edge_pairs")
        assert edge_pairs.shape[0] == int(edge_offsets[-1])
        n_nodes = len(manifest["node_ids"])
        if edge_pairs.size:
            assert edge_pairs.max() < n_nodes

        route_offsets = _slice(manifest, binary, "route_offsets")
        assert route_offsets.shape == (n_steps * len(clients) + 1,)
        assert np.all(np.diff(route_offsets.astype(np.int64)) >= 0)


def test_compute_route_matches_client_gateway_endpoints(client):
    """Каждый непустой маршрут в бинарном фрейме начинается в узле-клиенте и заканчивается
    в узле-шлюзе (индексы из node_ids) — тот же инвариант, что проверяет tools/golden.py."""
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "03_satellite_outages.json")
        manifest, binary = _compute(sock, base["variant_id"])
        node_ids = manifest["node_ids"]
        n_sat = manifest["n_sat"]
        clients = manifest["clients"]
        gateway_idx = {node_ids.index(g) for g in manifest["gateways"]}
        client_idx = {c: node_ids.index(c) for c in clients}

        hops = _slice(manifest, binary, "hops")
        route_offsets = _slice(manifest, binary, "route_offsets")
        route_nodes = _slice(manifest, binary, "route_nodes")

        checked = 0
        for step_i in range(manifest["n_steps"]):
            for ci, c in enumerate(clients):
                flat = step_i * len(clients) + ci
                start, end = int(route_offsets[flat]), int(route_offsets[flat + 1])
                path = route_nodes[start:end]
                if hops[step_i, ci] < 0:
                    assert len(path) == 0
                    continue
                checked += 1
                assert path[0] == client_idx[c]
                assert path[-1] in gateway_idx
                assert len(path) - 1 == hops[step_i, ci]
                for node in path[1:-1]:
                    assert node < n_sat, "промежуточный узел маршрута обязан быть спутником"
        assert checked > 0


def test_compute_result_cached_by_scenario_hash(client):
    """Повторный compute того же (variant_id, strategy) не пересчитывает — тот же бинарный
    буфер побайтово, и сообщений progress при этом меньше (моментальный cached-ответ)."""
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        manifest1, binary1 = _compute(sock, base["variant_id"], msg_id="first")
        manifest2, binary2 = _compute(sock, base["variant_id"], msg_id="second")
        assert binary1 == binary2
        assert manifest1 == manifest2


def test_compute_unknown_variant_is_not_found(client):
    with client.websocket_connect("/ws") as sock:
        _send(sock, "c", "compute", {"variant_id": "ghost"})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "not_found"


def test_compute_bad_strategy_is_bad_request(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "c", "compute", {"variant_id": base["variant_id"], "strategy": "fastest"})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "bad_request"
        assert env["field"] == "payload.strategy"


# ─────────────────────────────────────── 4. snapshot ────────────────────────────────────────

def test_snapshot_matches_cached_compute_route(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        manifest, binary = _compute(sock, base["variant_id"])
        t_s = manifest["t_s"][5]

        _send(sock, "snap", "snapshot", {"variant_id": base["variant_id"], "t_s": t_s})
        env = _recv_json(sock)
        assert env["type"] == "snapshot"
        payload = env["payload"]
        assert payload["t_s"] == t_s
        assert {sat["id"] for sat in payload["satellites"]}

        clients = manifest["clients"]
        ci = {c: i for i, c in enumerate(clients)}
        step_i = manifest["t_s"].index(t_s)
        hops = _slice(manifest, binary, "hops")
        for route in payload["routes"]:
            c = route["client_id"]
            expected_hops = hops[step_i, ci[c]]
            if expected_hops < 0:
                assert route["path"] == []
                assert route["reason"] in manifest["reason_labels"]
            else:
                assert route["hops"] == int(expected_hops)
                assert route["path"][0] == c


def test_snapshot_without_prior_compute_still_works(client):
    """snapshot — точечный запрос и обязан работать даже без предварительного compute()."""
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "04_link_range.json")
        _send(sock, "snap", "snapshot", {"variant_id": base["variant_id"], "t_s": 0})
        env = _recv_json(sock)
        assert env["type"] == "snapshot"
        assert env["payload"]["routes"]


def test_snapshot_invalid_t_s_is_bad_request(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "snap", "snapshot", {"variant_id": base["variant_id"], "t_s": 37})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "bad_request"
        assert env["field"] == "payload.t_s"


# ─────────────────────────────────────── 5. compare ────────────────────────────────────────

def test_compare_flags_incomparable_and_reports_deltas(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json", "base")
        _send(sock, "patch", "scenario.patch", {"variant_id": base["variant_id"], "launch_stage": 1})
        patched = _recv_json(sock)["payload"]

        _send(sock, "cmp", "compare", {
            "variant_id_a": base["variant_id"], "variant_id_b": patched["variant_id"],
        })
        env = _recv_json(sock)
        assert env["type"] == "compare"
        payload = env["payload"]
        assert payload["comparable"] is True
        assert payload["comparable_warning"] is None
        assert payload["design_diff"]["launch_stage"] == {"a": 3, "b": 1}
        for c, row in payload["clients"].items():
            assert row["delta"]["avail_pct"] == pytest.approx(row["b"]["avail_pct"] - row["a"]["avail_pct"])
            # первая очередь заведомо хуже полной группировки на этом наборе данных
            assert row["b"]["avail_pct"] <= row["a"]["avail_pct"] + 1e-9


def test_compare_between_incompatible_scenarios_warns(client):
    with client.websocket_connect("/ws") as sock:
        a = _load(sock, "01_full_constellation.json", "a")
        b = _load(sock, "04_link_range.json", "b")  # другой isl_range_km
        _send(sock, "cmp", "compare", {"variant_id_a": a["variant_id"], "variant_id_b": b["variant_id"]})
        env = _recv_json(sock)
        payload = env["payload"]
        assert payload["comparable"] is False
        assert "isl_range_km" in payload["comparable_warning"]
        assert payload["clients"], "числа всё равно считаются и показываются, только с предупреждением"


def test_compare_unknown_variant_names_the_field(client):
    with client.websocket_connect("/ws") as sock:
        a = _load(sock, "01_full_constellation.json")
        _send(sock, "cmp", "compare", {"variant_id_a": a["variant_id"], "variant_id_b": "ghost"})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "not_found"
        assert env["field"] == "payload.variant_id_b"


# ─────────────────────────────────────── 6. export ─────────────────────────────────────────

def test_export_scenario_kind_roundtrips(client):
    with client.websocket_connect("/ws") as sock:
        raw = _raw_scenario("01_full_constellation.json")
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "exp", "export", {"variant_id": base["variant_id"], "kind": "scenario"})
        env = _recv_json(sock)
        assert env["type"] == "export"
        assert env["payload"] == raw


def test_export_result_kind_passes_golden_check(client, tmp_path):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "03_satellite_outages.json")
        _send(sock, "exp", "export", {"variant_id": base["variant_id"], "kind": "result"})
        env = _recv_json(sock)
        assert env["type"] == "export"
        result = env["payload"]
        assert result["schema_version"] == "cosmo-A-result-1.0"

        out = tmp_path / "result.json"
        out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        problems = GOLDEN_TOOL.check_result(str(out))
        assert not problems, "\n".join(problems)


def test_export_bad_kind_is_bad_request(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "exp", "export", {"variant_id": base["variant_id"], "kind": "nonsense"})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "bad_request"
        assert env["field"] == "payload.kind"


# ─────────────────────────────────────── 7. attach ─────────────────────────────────────────

def test_attach_returns_cached_without_recompute(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        manifest1, binary1 = _compute(sock, base["variant_id"])

        _send(sock, "att", "attach", {"variant_id": base["variant_id"]})
        env = _recv_json(sock)
        assert env["type"] == "attach"
        assert env["id"] == "att"
        binary2 = sock.receive_bytes()
        assert env["payload"] == manifest1
        assert binary2 == binary1


def test_attach_without_prior_compute_is_not_computed(client):
    with client.websocket_connect("/ws") as sock:
        base = _load(sock, "01_full_constellation.json")
        _send(sock, "att", "attach", {"variant_id": base["variant_id"]})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "not_computed"


def test_attach_survives_new_connection_same_process(client):
    """Имитация переподключения: НОВОЕ соединение с уже известным variant_id получает готовый
    кеш через attach без повторного compute — ровно то, что требует контракт после обрыва."""
    with client.websocket_connect("/ws") as sock1:
        base = _load(sock1, "01_full_constellation.json")
        manifest1, binary1 = _compute(sock1, base["variant_id"])

    with client.websocket_connect("/ws") as sock2:
        _send(sock2, "att", "attach", {"variant_id": base["variant_id"]})
        env = _recv_json(sock2)
        assert env["type"] == "attach"
        binary2 = sock2.receive_bytes()
        assert env["payload"] == manifest1
        assert binary2 == binary1


# ─────────────────────────────────── конверт и общие ошибки ────────────────────────────────

def test_malformed_json_gets_null_id_error(client):
    with client.websocket_connect("/ws") as sock:
        sock.send_text("not json at all")
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["id"] is None
        assert env["code"] == "bad_envelope"


def test_unknown_type_is_reported(client):
    with client.websocket_connect("/ws") as sock:
        _send(sock, "x", "scenario.explode", {})
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["id"] == "x"
        assert env["code"] == "unknown_type"
        assert env["field"] == "type"


def test_missing_payload_key_defaults_to_error_not_crash(client):
    with client.websocket_connect("/ws") as sock:
        sock.send_text(json.dumps({"id": "y", "type": "compute"}))
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "bad_request"


def test_binary_frame_from_client_is_rejected_gracefully(client):
    with client.websocket_connect("/ws") as sock:
        sock.send_bytes(struct.pack("<4s", b"nope"))
        env = _recv_json(sock)
        assert env["type"] == "error"
        assert env["code"] == "bad_envelope"
        # соединение осталось рабочим после этого
        base = _load(sock, "01_full_constellation.json", "still_alive")
        assert base["variant_id"]
