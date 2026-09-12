"""Формат сообщений `/ws`: конверт, коды ошибок, бинарный фрейм результата расчёта.

Ничего не знает про FastAPI и про `core/` — чистые функции над `dict`/`bytes`, поэтому
проверяются без поднятия сервера. Веб-слой (`ws.py`) вызывает эти функции, сам решает,
какой обработчик вызвать для какого `type`, и держит состояние соединений/вариантов.

## Конверт

Любое сообщение в обе стороны — JSON-объект `{"id": str, "type": str, "payload": dict}`.
Ответ всегда несёт тот же `id`, что и запрос, — так клиент сопоставляет ответ со своим
запросом без очереди FIFO (`useWs.ts` резолвит промис по `id`). Единственное исключение —
сообщение, которое в принципе не удалось разобрать как `{id, type, payload}` (не JSON, не
объект, нет `id`): тогда `id` в ответе — `null`, разбирать больше нечего.

Семь типов запросов клиент → сервер (см. `.claude/rules/protocol.md`):

  analysis        payload = {variant_id} → резерв маршрутов, критические аппараты, кратность
                  покрытия и доставляемость с допуском по задержке (анализ устойчивости)
  scenario.load   payload = сырой JSON сценария целиком (объект `cosmo-A-1.0`)
                  → payload ответа = {variant_id, scenario_hash, summary, effective_scenario}
  scenario.patch  payload = {variant_id, launch_stage?, planes?, add_failures?, remove_failures?}
                  (поля патча — как у `core.scenario.patch_scenario`, слиты в один уровень
                  с variant_id, а не вложены под отдельным ключом)
                  → тот же формат ответа, что у scenario.load, но для НОВОГО variant_id
  compute         payload = {variant_id, strategy?: "hops"|"distance"}
                  → 0+ сообщений type="progress" с тем же id, затем ответ type="compute" —
                  JSON-манифест (см. ниже) плюс СРАЗУ ЗА НИМ один бинарный фрейм тем же id
                  неявно (порядок фреймов в сокете и есть привязка, у бинарного фрейма нет
                  собственного конверта). payload.binary === true — сигнал клиенту, что
                  следующий фрейм этого сокета — бинарные данные этого пакета.
  snapshot        payload = {variant_id, t_s, strategy?}
                  → payload = {variant_id, t_s, satellites, isl_edges, ground_contacts, routes}
                  чисто JSON, без бинарного фрейма — это точечный запрос для отладки/деталей,
                  а не замена бинарному пакету compute (см. .claude/rules/protocol.md).
  compare         payload = {variant_id_a, variant_id_b, strategy?}
                  → payload = {comparable, comparable_warning, environment_diff, design_diff,
                  clients: {client_id: {a, b, delta}}, clients_only_in_a, clients_only_in_b}
                  comparable=false, когда у вариантов различаются altitude_km/isl_range_km/
                  min_elevation_deg/step_s (docs/PARAMETERS.md) — числа всё равно посчитаны и
                  показаны, только с предупреждением, не молчаливым сравнением
  export          payload = {variant_id, kind?: "result"|"scenario" (по умолчанию "result"), strategy?}
                  → payload = САМ экспортируемый документ без обёртки (либо `cosmo-A-result-1.0`,
                  либо `cosmo-A-1.0`) — чтобы клиент мог сохранить `JSON.stringify(payload)` в
                  файл без разбора лишних полей.
  attach          payload = {variant_id, strategy?}
                  → тот же формат ответа, что у compute (манифест + бинарный фрейм), БЕЗ
                  пересчёта: только если результат уже в кеше, иначе — ошибка not_computed.

Ошибка — `{"id":, "type": "error", "code":, "field":, "message":}`. Коды:

  bad_envelope     — само сообщение не разобрать как {id, type, payload}
  unknown_type     — type не входит в девять известных запросов
  bad_request      — payload известного типа, но с неверной формой (нет ключа, не то значение)
  validation_error — сценарий/патч не прошёл проверку core.scenario (ScenarioError)
  not_found        — variant_id (или один из variant_id_a/variant_id_b) не существует
  not_computed     — attach запрошен для варианта без готового кеша (пересчёт запрещён контрактом)
  internal_error   — необработанное исключение; текст безопасен для показа (без трейсбека)

`field` — путь до проблемного места (`"payload.t_s"`, `"design.planes[1].raan_deg"`), а не
общее «что-то не так»: это прямое требование ТЗ к валидации входа.

## Бинарный фрейм `compute`/`attach`

Манифест (JSON) описывает один плоский `bytes`-буфер, который идёт вторым фреймом. Каждая
именованная секция — это массив numpy, сериализованный `tobytes()` (little-endian, как всегда
у numpy на x86/arm) и выровненный по границе 4 байта нулевым паддингом, чтобы JS мог построить
типизированный массив прямо поверх `ArrayBuffer` без копирования (`new Float32Array(buf, offset,
length/4)`) — выравнивание обязательно для Float32Array/Uint32Array, иначе браузер кидает
RangeError. `offset`/`length` в манифесте — уже с учётом паддинга/до паддинга соответственно
(offset всегда кратен 4, length — настоящий размер данных без хвостового паддинга).

Единое адресное пространство узлов на снимок: `node_ids = satellites + ground_sites`, спутники
первыми (индексы `0..n_sat-1`), затем наземные узлы (`n_sat..n_sat+n_ground-1`) — так рёбра И
маршруты кодируются одними и теми же uint16-индексами без второй таблицы соответствий. Является
ли ребро межспутниковым или наземным контактом, определяется по индексам на лету (оба конца
< n_sat → ISL, иначе — наземный контакт), отдельного поля-метки в бинарных данных нет.

Секции (имена ключей в `payload.layout`):
  sat_lla        float32 [n_steps, n_sat, 3]  — lat_deg, lon_deg, alt_ratio на каждом отсчёте
  sat_active     uint8   [n_steps, n_sat]     — 1 активен / 0 нет
  edge_offsets   uint32  [n_steps + 1]        — CSR: рёбра отсчёта t лежат в edge_pairs[edge_offsets[t]:edge_offsets[t+1]]
  edge_pairs     uint16  [n_edges_total, 2]   — индексы узлов обоих концов ребра
  hops           int16   [n_steps, n_clients] — число переходов маршрута; -1 = маршрута нет
  reason_codes   int8    [n_steps, n_clients] — индекс в payload.reason_labels, если hops=-1; иначе -1
  route_offsets  uint32  [n_steps*n_clients+1]— CSR по узлам маршрута, группировка (t*n_clients + c)
  route_nodes    uint16  [n_route_nodes_total]— индексы узлов маршрута client→...→gateway по порядку

`payload.clients`/`payload.node_ids` задают порядок столбцов/индексов выше; `payload.t_s` — сама
сетка секунд той же длины `n_steps`, `payload.client_metrics` — готовые доли/секунды по каждому
клиенту (`vis_pct`, `avail_pct`, `max_gap_s`, `gap_start_censored_s`, `gap_end_censored_s`) —
те же единицы, что отдаёт `core.metrics.compute` (доли 0..1, секунды), без форматирования в
проценты/минуты — это дело фронтенда.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

import numpy as np

__all__ = [
    "REQUEST_TYPES",
    "ERROR_CODES",
    "ProtocolError",
    "parse_message",
    "error_envelope",
    "progress_envelope",
    "result_envelope",
    "dumps",
    "pack_sections",
    "DTYPE_NAMES",
]

#: Семь типов запросов клиент → сервер — исчерпывающий список, см. .claude/rules/protocol.md.
REQUEST_TYPES: frozenset[str] = frozenset({
    "analysis",
    "variants.list",
    "scenario.load",
    "scenario.patch",
    "compute",
    "snapshot",
    "compare",
    "export",
    "attach",
})

#: Коды ошибок — исчерпывающий список, см. докстринг модуля.
ERROR_CODES: frozenset[str] = frozenset({
    "bad_envelope",
    "unknown_type",
    "bad_request",
    "validation_error",
    "not_found",
    "not_computed",
    "internal_error",
})

#: numpy dtype → имя, понятное JS (`new window[Float32Array](...)`), для поля `dtype` манифеста.
DTYPE_NAMES: dict[type, str] = {
    np.dtype("float32").type: "float32",
    np.dtype("uint8").type: "uint8",
    np.dtype("int8").type: "int8",
    np.dtype("uint16").type: "uint16",
    np.dtype("int16").type: "int16",
    np.dtype("uint32").type: "uint32",
    np.dtype("int32").type: "int32",
}


class ProtocolError(Exception):
    """Ошибка уровня конверта/payload (не расчёта) с кодом и именем проблемного поля.

    `envelope_id` может быть `None`, только если сам конверт не удалось разобрать настолько,
    что `id` неизвестен (не JSON, не объект, нет ключа `id`) — тогда ответ уходит с `id: null`.
    """

    def __init__(self, code: str, field: str, message: str, envelope_id: str | None = None):
        assert code in ERROR_CODES, f"неизвестный код ошибки протокола: {code!r}"
        self.code = code
        self.field = field
        self.message = message
        self.envelope_id = envelope_id
        super().__init__(f"[{code}] {field}: {message}")


def parse_message(raw_text: str) -> tuple[str, str, dict[str, Any]]:
    """Разбирает текстовый фрейм в `(id, type, payload)` или поднимает `ProtocolError`.

    Проверяется только форма конверта — семантика конкретного `payload` (обязательные ключи
    `variant_id`/`t_s`/...) остаётся обработчику этого `type` в `ws.py`.
    """
    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError("bad_envelope", "$", f"невалидный JSON: {exc}", envelope_id=None) from exc

    if not isinstance(raw, dict):
        raise ProtocolError("bad_envelope", "$", "сообщение должно быть JSON-объектом", envelope_id=None)

    msg_id = raw.get("id")
    if not isinstance(msg_id, str) or msg_id == "":
        raise ProtocolError("bad_envelope", "id", "поле id должно быть непустой строкой", envelope_id=None)

    msg_type = raw.get("type")
    if not isinstance(msg_type, str) or msg_type == "":
        raise ProtocolError("bad_envelope", "type", "поле type должно быть непустой строкой", envelope_id=msg_id)
    if msg_type not in REQUEST_TYPES:
        raise ProtocolError(
            "unknown_type", "type",
            f"неизвестный тип сообщения {msg_type!r}, ожидается один из {sorted(REQUEST_TYPES)}",
            envelope_id=msg_id,
        )

    payload = raw.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError("bad_envelope", "payload", "поле payload должно быть JSON-объектом", envelope_id=msg_id)

    return msg_id, msg_type, payload


def error_envelope(envelope_id: str | None, code: str, field: str, message: str) -> dict[str, Any]:
    assert code in ERROR_CODES, f"неизвестный код ошибки протокола: {code!r}"
    return {"id": envelope_id, "type": "error", "code": code, "field": field, "message": message}


def progress_envelope(envelope_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": envelope_id, "type": "progress", "payload": payload}


def result_envelope(envelope_id: str, msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Финальный ответ на запрос: тот же `type`, что был у запроса — так клиенту не нужна
    отдельная таблица «имя запроса → имя ответа», а `progress`/`error` легко отличить по type."""
    return {"id": envelope_id, "type": msg_type, "payload": payload}


def dumps(envelope: dict[str, Any]) -> str:
    """JSON-сериализация ответа. `allow_nan=False` — NaN/Inf в валидном сценарии невозможны
    (см. core.scenario._finite), так что их появление — признак бага, и лучше явная ошибка
    сериализации (перехватывается как internal_error), чем невалидный `NaN` в JSON на проводе."""
    return json.dumps(envelope, ensure_ascii=False, allow_nan=False)


def _pad4(n: int) -> int:
    """Ближайшее кратное 4, не меньшее `n` — граница выравнивания для Float32Array/Uint32Array."""
    return (n + 3) & ~3


def pack_sections(sections: Iterable[tuple[str, np.ndarray]]) -> tuple[bytes, dict[str, dict[str, Any]]]:
    """Склеивает именованные массивы numpy в один `bytes`-буфер с 4-байтовым выравниванием
    каждой секции и возвращает `(buffer, layout)`, где `layout[name] = {dtype, shape, offset,
    length}` — офсет всегда кратен 4 (годится для любого из используемых dtype), `length` —
    настоящий размер данных этой секции в байтах (без паддинга).

    Порядок секций в буфере — порядок `sections`; манифест хранит офсеты явно, поэтому порядок
    не обязан быть детерминированным сам по себе, но вызывающий код (ws.py) передаёт его
    фиксированным ради воспроизводимости пакетов при одинаковом входе.
    """
    chunks: list[bytes] = []
    layout: dict[str, dict[str, Any]] = {}
    offset = 0
    for name, arr in sections:
        arr = np.ascontiguousarray(arr)
        dtype_name = DTYPE_NAMES.get(arr.dtype.type)
        assert dtype_name is not None, f"секция {name!r}: неподдерживаемый dtype {arr.dtype}"
        raw = arr.tobytes()
        length = len(raw)
        layout[name] = {
            "dtype": dtype_name,
            "shape": list(arr.shape),
            "offset": offset,
            "length": length,
        }
        padded = _pad4(length)
        chunks.append(raw)
        if padded > length:
            chunks.append(b"\x00" * (padded - length))
        offset += padded
    return b"".join(chunks), layout
