# Протокол `/ws`

Единственный расчётный канал сервиса — один WebSocket-эндпоинт `/ws`. REST-ручек с вычислениями
нет: `/health` — только healthcheck контейнера, `/samples`/`/samples/{name}` — раздача JSON-сэмплов
из `Данные/` для кнопки «пример» в UI, статика — собранный фронтенд. Документ описывает протокол
по фактическому коду: `backend/app/protocol.py` (конверт, коды ошибок, бинарная упаковка) и
`backend/app/ws.py` (семь обработчиков). При изменении протокола правьте этот файл в том же
коммите — иначе он расходится с кодом молча.

## Конверт

Любое сообщение в обе стороны — JSON-объект:

```json
{"id": "1", "type": "scenario.load", "payload": { /* зависит от type */ }}
```

- `id` — произвольная непустая строка, которую задаёт клиент. Ответ всегда несёт тот же `id`, это
  единственный способ сопоставить ответ запросу (соединение не гарантирует порядок ответов при
  нескольких одновременных запросах на разных сокетах — на одном сокете сервер обрабатывает
  сообщения строго последовательно, см. «Порядок обработки» ниже).
- `type` — один из семи типов запроса (таблица ниже).
- `payload` — объект, форма зависит от `type`; можно опустить (тогда берётся `{}`).

Если сам конверт не разобрать (не JSON, не объект, нет `id`), ответ уходит с `"id": null` —
разбирать дальше нечего.

## Типы сообщений

| `type` запроса | `payload` запроса | `payload` ответа |
|---|---|---|
| `scenario.load` | сырой JSON сценария `cosmo-A-1.0` целиком (без обёртки `{"scenario": …}`) | `{variant_id, scenario_hash, effective_scenario, summary}` |
| `scenario.patch` | `{variant_id, launch_stage?, planes?, add_failures?, remove_failures?}` | тот же формат, что у `scenario.load`, плюс `parent_variant_id`, для **нового** `variant_id` |
| `compute` | `{variant_id, strategy?: "hops"\|"distance"}` (по умолчанию `"hops"`) | 0+ сообщений `type:"progress"` с тем же `id`, затем манифест `type:"compute"` и сразу следом один бинарный фрейм |
| `snapshot` | `{variant_id, t_s, strategy?}` | `{variant_id, t_s, satellites, isl_edges, ground_contacts, routes}` — чистый JSON, без бинарного фрейма |
| `compare` | `{variant_id_a, variant_id_b, strategy?}` | `{comparable, comparable_warning, environment_diff, design_diff, clients, clients_only_in_a, clients_only_in_b}` |
| `export` | `{variant_id, kind?: "result"\|"scenario", strategy?}` (по умолчанию `"result"`) | сам экспортируемый документ **без обёртки** — либо `cosmo-A-result-1.0`, либо `cosmo-A-1.0` |
| `attach` | `{variant_id, strategy?}` | тот же формат, что у `compute` (манифест + бинарный фрейм), **без пересчёта** |
| `analysis` | `{variant_id}` | `{reserve, critical_satellites, coverage, delivery}` — анализ устойчивости, считается отдельным проходом и кешируется по варианту |
| `variants.list` | `{}` | `{variants: [{variant_id, title, source, created, summary}]}` — сохранённые на диске конфигурации; переживают перезапуск сервиса |

Сценарии в сервисе неизменяемы: `scenario.load`/`scenario.patch` не правят существующий вариант, а
создают новый и возвращают его `variant_id`. `variant_id == scenario_hash` — SHA-256 от
`environment`+`design`+`ground_sites`+`failures`+`gateway_outages` (без `meta`, косметическая
правка названия не должна сбрасывать кеш расчёта). Отсюда следует: повторная загрузка/патч,
давшие по смыслу тот же сценарий, возвращают тот же `variant_id` без дублирования, и «сохранить
вариант и вернуться к нему» (DoD п.1) реализуется на клиенте просто как список запомненных
`variant_id`.

### `scenario.load` — пример ответа (сокращённо)

```json
{
  "id": "1", "type": "scenario.load",
  "payload": {
    "variant_id": "9394d66fa12e75cb9f321567a424af6071cc0644d39a42c089e90691188fd57e",
    "scenario_hash": "9394d66fa12e75cb9f321567a424af6071cc0644d39a42c089e90691188fd57e",
    "effective_scenario": { "schema_version": "cosmo-A-1.0", "meta": {"id": "01_full_constellation", "title": "Полная группировка"}, "environment": {"...": "..."}, "design": {"...": "..."}, "ground_sites": ["..."], "failures": [], "gateway_outages": [] },
    "summary": {
      "n_planes": 3, "n_satellites": 48, "n_ground_sites": 4, "n_clients": 3, "n_gateways": 1,
      "launch_stage": 3, "horizon_s": 86400, "step_s": 120, "n_steps": 720,
      "altitude_km": 550.0, "inclination_deg": 87.0, "min_elevation_deg": 10.0,
      "isl_range_km": 3000.0, "target_availability": 0.9, "n_failures": 0, "n_gateway_outages": 0
    }
  }
}
```

`summary` не содержит ни одного зашитого ID — форма и счётчики берутся из самого загруженного
сценария, поэтому работает одинаково на файле жюри с другими именами пунктов и плоскостей.

### `scenario.patch` — правка конфигурации

```json
{"id": "6", "type": "scenario.patch",
 "payload": {"variant_id": "9394d6…fd57e", "launch_stage": 2}}
```

Формат `patch` (поля опциональны, комбинируются в одном запросе):

```json
{
  "launch_stage": 2,
  "planes": {"P1": {"raan_deg": 30.0}, "P2": {"raan_deg": 90.0, "phase_deg": 10.0}},
  "add_failures": [{"satellite_id": "S01", "start_s": 0, "end_s": 3600}],
  "remove_failures": [{"satellite_id": "S01", "start_s": 0, "end_s": 3600}]
}
```

Границы (сервер — единственный источник истины, UI обязан их отражать, но не полагаться только на
клиентскую проверку): `launch_stage` — целое 1, 2 или 3; `raan_deg`/`phase_deg` — конечное число в
`[0, 360)` (360 недопустимо); `add_failures`/`remove_failures` — элемент сопоставляется целиком
(`satellite_id`+`start_s`+`end_s`, у отказа нет отдельного ID). Ответ — новый `variant_id`,
`parent_variant_id` указывает, от какого варианта сделана правка:

```json
{"variant_id": "aa0798…694c7", "scenario_hash": "aa0798…694c7",
 "parent_variant_id": "9394d6…fd57e", "effective_scenario": {"...": "..."}, "summary": {"...": "..."}}
```

### `compute` / `attach` — расчёт горизонта одним пакетом

`compute` считает все 720 отсчётов сразу и кеширует результат в памяти процесса по ключу
`(variant_id, strategy)`. Пока расчёт идёт, сервер шлёт сообщения прогресса с тем же `id`:

```json
{"id": "8", "type": "progress", "payload": {"variant_id": "9394d6…fd57e", "strategy": "hops", "pct": 45, "stage": "metrics_done"}}
```

Затем — финальный ответ `type:"compute"`: JSON-манифест текстовым фреймом, **сразу за ним** один
бинарный фрейм того же сокета (привязка через порядок фреймов, у бинарного фрейма нет своего
конверта; `payload.binary === true` — явный сигнал клиенту, что следующий фрейм — бинарные данные
этого пакета). `attach` отдаёt тот же формат ответа **без пересчёта**: если кеша `(variant_id,
strategy)` ещё нет — ошибка `not_computed`, а не тихий запуск заново. Это и есть поведение при
реконнекте: клиент переподключился — шлёт `attach`, получает готовый пакет.

Манифест (пример по `01_full_constellation.json`, секции `layout` сокращены):

```json
{
  "variant_id": "9394d6…fd57e", "scenario_hash": "9394d6…fd57e", "strategy": "hops",
  "step_s": 120, "n_steps": 720, "t_s": [0, 120, "...", 86280],
  "node_ids": ["S01", "...", "S48", "C65", "C70", "C72", "G_MUR"],
  "n_sat": 48, "n_ground": 4,
  "clients": ["C65", "C70", "C72"], "gateways": ["G_MUR"],
  "client_metrics": {
    "C65": {"vis_pct": 0.9777777777777777, "avail_pct": 0.9666666666666667, "max_gap_s": 480, "gap_start_censored_s": 0, "gap_end_censored_s": 0}
  },
  "reason_labels": ["no_visible_satellite", "isl_partition", "no_gateway_contact", "gateway_offline"],
  "binary": true,
  "layout": {
    "sat_lla":       {"dtype": "float32", "shape": [720, 48, 3], "offset": 0,       "length": 414720},
    "sat_active":    {"dtype": "uint8",   "shape": [720, 48],    "offset": 414720,  "length": 34560},
    "edge_offsets":  {"dtype": "uint32",  "shape": [721],        "offset": "...",   "length": 2884},
    "edge_pairs":    {"dtype": "uint16",  "shape": ["n_edges_total", 2], "offset": "...", "length": "..."},
    "hops":          {"dtype": "int16",   "shape": [720, 3],     "offset": "...",   "length": 4320},
    "visible":       {"dtype": "uint8",   "shape": [720, 3],     "offset": "...",   "length": 2160},
    "reason_codes":  {"dtype": "int8",    "shape": [720, 3],     "offset": "...",   "length": 2160},
    "route_offsets": {"dtype": "uint32",  "shape": [2161],       "offset": "...",   "length": 8644},
    "route_nodes":   {"dtype": "uint16",  "shape": ["n_route_nodes_total"], "offset": "...", "length": "..."}
  }
}
```

`client_metrics[c].avail_pct/vis_pct` — доли `0..1` (не проценты, форматирование — дело фронтенда),
`max_gap_s`/`gap_*_censored_s` — секунды. `gap_start_censored_s`/`gap_end_censored_s` — перерыв,
упирающийся в начало/конец горизонта: он короче настоящего (усечён границей), поэтому показан
отдельно от `max_gap_s`, а не подмешан в него молча.

**Бинарный фрейм.** Один плоский `bytes`-буфер: секции из `layout` идут одна за другой, каждая
выровнена на границу 4 байта нулевым паддингом (`offset` в `layout` всегда кратен 4; `length` —
настоящий размер без паддинга) — так JS строит типизированный массив прямо поверх `ArrayBuffer` без
копирования (`new Float32Array(buf, offset, length/4)`), что для `Float32Array`/`Uint32Array`
обязано быть выровнено, иначе браузер кидает `RangeError`.

Единое адресное пространство узлов на снимок — `node_ids = satellites + ground_sites`, спутники
первыми (`0..n_sat-1`), затем наземные узлы (`n_sat..`): рёбра и маршруты кодируются одними и теми
же `uint16`-индексами без второй таблицы соответствий, тип ребра (ISL или наземный контакт)
определяется по индексам на лету (оба конца `< n_sat` → ISL).

| Секция | dtype | форма | смысл |
|---|---|---|---|
| `sat_lla` | `float32` | `[n_steps, n_sat, 3]` | `lat_deg, lon_deg, alt_ratio` на каждом отсчёте |
| `sat_active` | `uint8` | `[n_steps, n_sat]` | 1 активен / 0 нет |
| `edge_offsets` | `uint32` | `[n_steps+1]` | CSR: рёбра отсчёта `t` — это `edge_pairs[edge_offsets[t]:edge_offsets[t+1]]` |
| `edge_pairs` | `uint16` | `[n_edges_total, 2]` | индексы узлов обоих концов ребра |
| `hops` | `int16` | `[n_steps, n_clients]` | число переходов маршрута; `-1` = маршрута нет |
| `visible` | `uint8` | `[n_steps, n_clients]` | 1, если у пункта есть контакт хотя бы с одним активным спутником |
| `reason_codes` | `int8` | `[n_steps, n_clients]` | индекс в `payload.reason_labels`, если `hops=-1`; иначе `-1` |
| `route_offsets` | `uint32` | `[n_steps*n_clients+1]` | CSR по узлам маршрута, группировка `t*n_clients + c` |
| `route_nodes` | `uint16` | `[n_route_nodes_total]` | индексы узлов маршрута `client → … → gateway` по порядку |

Замер на `01_full_constellation.json`: позиции `720×48×3` `float32` ≈ 405 КБ, рёбер в среднем ~81
на отсчёт (~58 тыс. за сутки) как пары `uint16` ещё ~227 КБ — весь пакет укладывается заметно
меньше мегабайта на вариант (фактически ~0.7 МБ), что и позволяет скрабить временную шкалу без
единого дополнительного запроса к серверу.

### `snapshot` — точечный запрос момента

```json
{"id": "2", "type": "snapshot", "payload": {"variant_id": "9394d6…fd57e", "t_s": 0}}
```

`t_s` обязан быть узлом сетки (`0, step_s, …, horizon_s-step_s`), иначе — `bad_request`. Если для
`(variant_id, strategy)` уже есть кеш `compute`, маршруты берутся из него; если нет — считаются на
лету тем же `core.routing.find_route` без сохранения в кеш. Отдельная ручка для отладки/деталей —
не замена бинарному пакету `compute` для отрисовки всего горизонта.

```json
{
  "variant_id": "9394d6…fd57e", "t_s": 0,
  "satellites": [{"id": "S01", "x_km": "...", "y_km": "...", "z_km": "...", "lat_deg": "...", "lon_deg": "...", "alt_ratio": 0.086, "active": true}],
  "isl_edges": [{"a": "S01", "b": "S02", "dist_km": 2699.9}],
  "ground_contacts": [{"ground_id": "C65", "satellite_id": "S20", "dist_km": "...", "elevation_deg": 23.4}],
  "routes": [
    {"client_id": "C65", "gateway_id": "G_MUR", "path": ["C65", "S20", "G_MUR"], "hops": 2, "reason": null},
    {"client_id": "C70", "gateway_id": "G_MUR", "path": ["C70", "S20", "G_MUR"], "hops": 2, "reason": null}
  ]
}
```

Когда маршрута нет, `path: []`, `hops: null`, `gateway_id: null`, `reason` — одна из четырёх причин
ТЗ (см. «Причины отсутствия маршрута» ниже).

### `compare` — сравнение двух сохранённых вариантов

```json
{"id": "10", "type": "compare", "payload": {"variant_id_a": "9394d6…fd57e", "variant_id_b": "aa0798…694c7"}}
```

Оба варианта досчитываются (`compute`, если кеша ещё нет), поэтому ответ может занять время первого
расчёта. `comparable=false`, если у вариантов различаются параметры, обязательные для сопоставимости
метрик (`altitude_km`, `isl_range_km`, `min_elevation_deg`, `step_s` — см. `docs/PARAMETERS.md`) —
числа всё равно считаются и показываются честно, `comparable_warning` объясняет почему вывод
«вариант B лучше A» по ним делать нельзя. `delta` считается как `b − a`.

```json
{
  "variant_id_a": "9394d6…fd57e", "variant_id_b": "aa0798…694c7", "strategy": "hops",
  "comparable": true, "comparable_warning": null,
  "environment_diff": {},
  "design_diff": {"launch_stage": {"a": 3, "b": 2}},
  "clients": {
    "C65": {
      "a": {"vis_pct": 0.978, "avail_pct": 0.967, "max_gap_s": 480, "gap_start_censored_s": 0, "gap_end_censored_s": 0},
      "b": {"vis_pct": 0.717, "avail_pct": 0.618, "max_gap_s": 19800, "gap_start_censored_s": 0, "gap_end_censored_s": 0},
      "delta": {"vis_pct": -0.261, "avail_pct": -0.349, "max_gap_s": 19320}
    }
  },
  "clients_only_in_a": [], "clients_only_in_b": []
}
```

`clients_only_in_a`/`clients_only_in_b` — пункты, которых нет в другом варианте (жюри может
сравнивать сценарии с разным составом пунктов), тогда они не попадают в `clients`, а перечислены
здесь отдельно, чтобы UI не молчал о них.


### `analysis` — анализ устойчивости

Обязательные показатели отвечают на вопрос «есть ли путь». Этот запрос отвечает на вопрос критерия
«Анализ устойчивости»: сколько отказов путь переживёт и чем это ограничено. Проход по горизонту
занимает единицы секунд (на полной группировке ≈1.5 с), поэтому он вынесен в отдельное сообщение,
а не встроен в `compute`, и результат кешируется по `variant_id`.

```json
{"id": "12", "type": "analysis", "payload": {"variant_id": "9394d6…fd57e"}}
```

Ответ:

```json
{
  "variant_id": "9394d6…fd57e",
  "reserve": {
    "C65": {"histogram": {"0": 0.033, "1": 0.801, "2": 0.158, "3": 0.007},
            "reserve_pct": 0.1653, "mean": 1.14}
  },
  "critical_satellites": [{"satellite_id": "S02", "count": 45}],
  "coverage": {"G_MUR": {"histogram": {"0": 0.011, "1": 0.476, "2": 0.432, "3": 0.081}, "mean": 1.58}},
  "delivery": {
    "tolerances_s": [0, 120, 300, 600, 1800, 3600],
    "clients": {"C65": {"delivered_pct": {"0": 0.9667, "120": 0.9778}, "delay_s": ["…"]}}
  }
}
```

- `reserve[c].reserve_pct` — доля отсчётов, где до шлюза существуют **два и более**
  вершинно-непересекающихся маршрута (теорема Менгера, максимальный поток при единичной
  пропускной способности аппаратов). Один маршрут означает, что отказ одного аппарата рвёт связь.
- `critical_satellites[].count` — в скольких отсчётах изъятие аппарата рвёт связь хотя бы одному пункту.
- `coverage[node]` — сколько активных аппаратов узел видит одновременно; резерв упирается именно
  в наземные линии, поэтому считается и по шлюзам, а не только по клиентам.
- `delivery` — доля отсчётов, из которых данные дойдут до шлюза при допуске по задержке
  (ожидание на борту, временной граф). Допуск 0 обязан совпасть со сквозной доступностью
  из `compute`; расхождение означает ошибку расчёта.

Все доли — 0..1, как и в остальных ответах. Обоснование самих показателей — `docs/EXTENSIONS.md`.

### `export` — выгрузка результата или изменённого сценария

```json
{"id": "4", "type": "export", "payload": {"variant_id": "9394d6…fd57e", "kind": "result"}}
```

`kind: "result"` (по умолчанию) — выгрузка расчёта, `cosmo-A-result-1.0`:

```json
{
  "schema_version": "cosmo-A-result-1.0",
  "effective_scenario": {"schema_version": "cosmo-A-1.0", "...": "..."},
  "routes": [{"t_s": 0, "client_id": "C65", "path": ["C65", "S20", "G_MUR"]}, {"t_s": 120, "client_id": "C65", "path": []}]
}
```

По одной записи на каждую пару `(t_s, client_id)` — ровно `n_steps × n_clients`, пустой `path` =
маршрута нет (720×3 = 2160 записей на `01_full_constellation.json`, проверено `tools/golden.py
--result`). `kind: "scenario"` — отдельная выгрузка **изменённого** сценария `cosmo-A-1.0` (для
повторной загрузки; DoD «загрузка нового сценария того же формата»), не то же самое, что выгрузка
результата.

## Причины отсутствия маршрута

Ровно четыре, по возрастанию «глубины» диагноза (`backend/core/routing.py::no_route_reason`),
отдаются в `reason_codes` бинарного пакета (индекс в `payload.reason_labels`) и в `snapshot.routes[].reason`:

| `reason` | Значит |
|---|---|
| `no_visible_satellite` | у пункта нет контакта ни с одним активным спутником прямо сейчас — проблема в покрытии |
| `isl_partition` | спутник виден, но межспутниковая сеть, достижимая от него, не дотягивается ни до одного шлюза, хотя где-то в группировке контакт со шлюзом в этот момент есть — сеть разбита на изолированные части («видимость ≠ маршрут») |
| `no_gateway_contact` | контакта со шлюзом нет нигде в группировке прямо сейчас — геометрия, не отказ |
| `gateway_offline` | контакт был бы геометрически, но шлюз в этот момент в `gateway_outages` |

## Ошибки

```json
{"id": "2", "type": "error", "code": "bad_request", "field": "payload.t_s", "message": "t_s должен быть узлом сетки: 0, 120, ..., 86280 (шаг 120)"}
```

| `code` | Когда |
|---|---|
| `bad_envelope` | само сообщение не разобрать как `{id, type, payload}` (не JSON, не объект, нет `id`/`type`) |
| `unknown_type` | `type` не входит в семь известных запросов |
| `bad_request` | `payload` известного типа, но неверной формы (нет ключа, не то значение, `t_s` не на сетке, неизвестная `strategy`) |
| `validation_error` | сценарий/патч не прошёл валидацию `core.scenario` (`ScenarioError`) |
| `not_found` | `variant_id` (или один из `variant_id_a`/`variant_id_b`) не существует |
| `not_computed` | `attach` запрошен для варианта без готового кеша — пересчёт по контракту запрещён |
| `internal_error` | необработанное исключение; текст в `message` безопасен для показа (без трейсбека) |

`field` — путь до проблемного места (`payload.t_s`, `design.planes[1].raan_deg`,
`patch.planes.P9`) — это прямое требование ТЗ к валидации входа, а не общее «что-то не так».
Примеры, снятые с реального ответа сервера:

```json
{"id": "3", "type": "error", "code": "not_found", "field": "payload.variant_id", "message": "вариант 'doesnotexist' не найден — сначала пришлите scenario.load или scenario.patch"}
{"id": "4", "type": "error", "code": "unknown_type", "field": "type", "message": "неизвестный тип сообщения 'bogus', ожидается один из [...]"}
{"id": "5", "type": "error", "code": "validation_error", "field": "environment.min_elevation_deg", "message": "должно быть в диапазоне [0, 90) град."}
{"id": "7", "type": "error", "code": "not_computed", "field": "payload.variant_id", "message": "для варианта '...' (strategy='hops') ещё нет готового расчёта — attach не пересчитывает, сначала отправьте compute"}
```

## Порядок обработки и реконнект

- Сообщения одного соединения обрабатываются **строго последовательно**: следующее читается,
  только когда предыдущее полностью отправлено (включая возможный бинарный фрейм) — это исключает
  переплетение двух пар «манифест + бинарный фрейм» на проводе, если клиент шлёт запросы быстрее,
  чем сервер отвечает.
- Расчёт (`core.metrics.compute` + сбор геометрии по всем отсчётам) выполняется в отдельном потоке
  (`asyncio.to_thread`) — event loop не блокируется, `progress` доходит до клиента, а другие
  соединения к тому же серверу продолжают отвечать во время долгого расчёта.
- Если два соединения одновременно запросили `compute` для одного и того же `(variant_id,
  strategy)`, считается один раз — второй запрос ждёт уже идущий расчёт, а не дублирует его.
- Результат живёт в памяти процесса, пока сервис не перезапущен (не файл/БД, см. докстринг
  `backend/app/ws.py::_Store`) — этого достаточно, чтобы «attach после обрыва соединения» работал
  без пересчёта. Переживание перезапуска процесса контрактом не требуется.
- Разрыв сокета клиент видит стандартным событием `close`; при новом подключении — `attach` с тем
  же `variant_id`, что и был, если он ещё нужен, иначе — новый `scenario.load`/`scenario.patch`.

## CORS и локальная разработка

Прод-сборка (`docker compose up`) отдаёт фронтенд и `/ws` с одного origin — CORS не участвует.
Для `nuxt dev` на отдельном порту, где фронтенд ходит на бэкенд напрямую, сервер разрешает любой
`Origin` (`backend/app/main.py`); адрес `/ws` и REST-ручек переопределяется переменными окружения
`NUXT_PUBLIC_WS_URL`/`NUXT_PUBLIC_API_URL` (`frontend/nuxt.config.ts`).
