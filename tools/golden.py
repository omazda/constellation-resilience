#!/usr/bin/env python3
"""Эталон и проверка. Источник истины — «Расчетный модуль/geometry.py».

  python3 tools/golden.py                        # эталонные метрики по всем сценариям
  python3 tools/golden.py Данные/01_*.json       # по конкретным
  python3 tools/golden.py --result out.json      # проверить выгрузку cosmo-A-result-1.0

Доля доступности и максимальный перерыв — это достижимость в графе, она не зависит
от алгоритма маршрутизации. Расхождение с эталоном = ошибка расчётной модели.
"""
from __future__ import annotations
import sys, json, collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Расчетный модуль"))
import geometry as G


def grid(s):
    e = s["environment"]
    return list(range(0, int(e["horizon_s"]), int(e["step_s"])))


def adjacency(s, t):
    adj = collections.defaultdict(set)
    for a, b, _ in G.snapshot(s, t)["edges"]:
        adj[a].add(b); adj[b].add(a)
    return adj


def metrics(s):
    """{client: {vis_pct, avail_pct, max_gap_min}} — эталон по сетке отсчётов."""
    sat_ids = {x["id"] for x in s["design"]["satellites"]}
    gw_ids = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    clients = [g["id"] for g in s["ground_sites"] if g["role"] == "client"]
    steps = grid(s)
    vis = dict.fromkeys(clients, 0); ok = dict.fromkeys(clients, 0)
    gap = dict.fromkeys(clients, 0); cur = dict.fromkeys(clients, 0)
    for t in steps:
        adj = adjacency(s, t)
        for c in clients:
            if adj[c] & sat_ids:
                vis[c] += 1
            seen, q, found = {c}, collections.deque([c]), False
            while q and not found:                      # наземные пункты не ретранслируют
                for nb in adj[q.popleft()]:
                    if nb in gw_ids: found = True; break
                    if nb in sat_ids and nb not in seen: seen.add(nb); q.append(nb)
            if found: ok[c] += 1; cur[c] = 0
            else: cur[c] += 1; gap[c] = max(gap[c], cur[c])
    n, step = len(steps), s["environment"]["step_s"]
    return {c: {"vis_pct": round(100 * vis[c] / n, 2),
                "avail_pct": round(100 * ok[c] / n, 2),
                "max_gap_min": round(gap[c] * step / 60, 1)} for c in clients}


def check_result(path):
    """Проверка выгрузки: формат, полнота, физическая допустимость каждого маршрута."""
    res = json.loads(Path(path).read_text(encoding="utf-8"))
    err = []
    if res.get("schema_version") != "cosmo-A-result-1.0":
        err.append(f"schema_version = {res.get('schema_version')!r}, ожидается 'cosmo-A-result-1.0'")
    s = res.get("effective_scenario")
    if not isinstance(s, dict):
        err.append("нет effective_scenario"); return err
    G.validate(s)
    sat_ids = {x["id"] for x in s["design"]["satellites"]}
    gw_ids = {g["id"] for g in s["ground_sites"] if g["role"] == "gateway"}
    clients = {g["id"] for g in s["ground_sites"] if g["role"] == "client"}
    steps = grid(s)
    routes = res.get("routes") or []
    seen = collections.Counter((r.get("t_s"), r.get("client_id")) for r in routes)
    need = {(t, c) for t in steps for c in clients}
    miss, extra = need - set(seen), set(seen) - need
    if miss: err.append(f"нет записей для {len(miss)} пар (t_s, client_id), напр. {sorted(miss)[:3]}")
    if extra: err.append(f"лишние/невалидные пары: {len(extra)}, напр. {sorted(extra, key=str)[:3]}")
    if any(v > 1 for v in seen.values()): err.append("дубли записей на пару (t_s, client_id)")

    by_t = collections.defaultdict(list)
    for r in routes:
        if r.get("path"): by_t[r["t_s"]].append(r)
    bad, built = 0, collections.Counter()
    for t in sorted(by_t):
        if t not in steps: continue
        adj = adjacency(s, t)
        for r in by_t[t]:
            p = r["path"]
            why = None
            if p[0] != r["client_id"]: why = "путь не начинается в client_id"
            elif p[-1] not in gw_ids: why = "путь не заканчивается в шлюзе"
            elif any(x not in sat_ids for x in p[1:-1]): why = "промежуточный узел не спутник"
            elif any(b not in adj[a] for a, b in zip(p, p[1:])): why = "в пути есть несуществующая в этот момент связь"
            if why:
                bad += 1
                if bad <= 3: err.append(f"t_s={t} {r['client_id']}: {why}: {p}")
            else:
                built[r["client_id"]] += 1
    if bad > 3: err.append(f"…всего недопустимых маршрутов: {bad}")

    ref = metrics(s)
    for c in sorted(clients):
        got = round(100 * built[c] / len(steps), 2)
        if abs(got - ref[c]["avail_pct"]) > 0.01:
            err.append(f"{c}: доступность по выгрузке {got}%, эталон {ref[c]['avail_pct']}% "
                       f"({'пропущены существующие маршруты' if got < ref[c]['avail_pct'] else 'заявлены несуществующие'})")
    return err


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--result":
        problems = check_result(args[1])
        print("\n".join("FAIL " + p for p in problems) if problems else "OK — выгрузка соответствует ТЗ")
        sys.exit(1 if problems else 0)
    files = args or sorted(str(p) for p in (Path(__file__).resolve().parent.parent / "Данные").glob("*.json"))
    for f in files:
        s = G.load(f)
        print(f"\n{Path(f).name}  ({len(grid(s))} отсчётов, цель {s['environment']['target_availability']:.0%})")
        print(f"  {'пункт':<8}{'видимость':>11}{'доступность':>13}{'макс. перерыв':>15}")
        for c, m in metrics(s).items():
            flag = "" if m["avail_pct"] >= 100 * s["environment"]["target_availability"] else "  ← ниже цели"
            print(f"  {c:<8}{m['vis_pct']:>10.2f}%{m['avail_pct']:>12.2f}%{m['max_gap_min']:>12.0f} мин{flag}")
