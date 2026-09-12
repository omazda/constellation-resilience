/**
 * Пакет результата расчёта: манифест `compute`/`attach` плюс бинарный фрейм, разобранный в
 * типизированные массивы прямо поверх `ArrayBuffer` без копирования (docs/PROTOCOL.md).
 *
 * Отсюда берутся сетка отсчётов, ряды доступности по каждому клиентскому пункту и сводные
 * показатели — то есть всё, что рисуют `TimelineBar.vue` и `AvailabilityChart.vue`, без единого
 * обращения к серверу при перемотке.
 *
 * Мгновенное состояние сети для глобуса (`isl_edges` с расстояниями, `ground_contacts` с углом
 * возвышения) в бинарный пакет не входит — там только индексы узлов рёбер. Поэтому снимок берётся
 * запросом `snapshot` и кешируется по индексу отсчёта: это ровно тот случай, для которого `snapshot`
 * в контракте и оставлен.
 */
import type { AvailabilityClientSeries } from '../components/AvailabilityChart.vue'

export interface ResultSection {
  dtype: 'float32' | 'uint8' | 'int8' | 'uint16' | 'int16' | 'uint32'
  shape: number[]
  offset: number
  length: number
}

export interface ResultManifest {
  variant_id: string
  strategy: string
  step_s: number
  n_steps: number
  t_s: number[]
  node_ids: string[]
  n_sat: number
  n_ground: number
  clients: string[]
  gateways: string[]
  client_metrics: Record<string, {
    vis_pct: number
    avail_pct: number
    max_gap_s: number
    gap_start_censored_s: number
    gap_end_censored_s: number
  }>
  reason_labels: string[]
  binary: true
  layout: Record<string, ResultSection>
  buffer: ArrayBuffer
}

/** `length` в манифесте — настоящий размер секции в байтах, без выравнивающего паддинга. */
function view(buffer: ArrayBuffer, s: ResultSection) {
  switch (s.dtype) {
    case 'float32': return new Float32Array(buffer, s.offset, s.length / 4)
    case 'uint32': return new Uint32Array(buffer, s.offset, s.length / 4)
    case 'uint16': return new Uint16Array(buffer, s.offset, s.length / 2)
    case 'int16': return new Int16Array(buffer, s.offset, s.length / 2)
    case 'uint8': return new Uint8Array(buffer, s.offset, s.length)
    case 'int8': return new Int8Array(buffer, s.offset, s.length)
  }
}

/** Кадр сети на одном отсчёте, собранный из пакета — ровно то, что ждёт GlobeView. */
export interface Frame {
  satellites: { id: string, lat_deg: number, lon_deg: number, alt_ratio: number, active: boolean }[]
  islEdges: { a: string, b: string }[]
  groundContacts: { ground_id: string, satellite_id: string }[]
  routes: { client_id: string, gateway_id: string | null, path: string[], hops: number | null, reason: string | null }[]
}

export interface DecodedResult {
  steps: number[]
  stepS: number
  clients: string[]
  series: AvailabilityClientSeries[]
  /** Число переходов маршрута на отсчёте, `null` если маршрута нет — для подписи на шкале. */
  hopsAt: (index: number, clientId: string) => number | null
  /**
   * Состояние сети на отсчёте — собирается из уже полученных массивов, без обращения к серверу.
   * Это и есть причина, по которой перемотка мгновенная: весь горизонт уже в памяти браузера.
   */
  frameAt: (index: number) => Frame
  /**
   * След аппарата: его подспутниковые точки за `back` предыдущих отсчётов. Нужен, чтобы движение
   * читалось на паузе — по статичной точке невозможно понять, куда летит аппарат.
   */
  trailAt: (index: number, back: number) => { id: string, points: [number, number, number][] }[]
  /** Период повторения межспутниковой топологии в секундах — половина периода обращения
   *  (docs/MODEL.md). Рисуется насечками на полосе суток: структура сети видна глазом. */
  islPeriodS: number
}

export function decodeResult(
  m: ResultManifest,
  labels: Record<string, string> = {},
  altitudeKm = 550
): DecodedResult {
  const nSteps = m.n_steps
  const nClients = m.clients.length

  const hops = view(m.buffer, m.layout.hops!) as Int16Array
  const visible = view(m.buffer, m.layout.visible!) as Uint8Array
  const reasonCodes = view(m.buffer, m.layout.reason_codes!) as Int8Array

  const series: AvailabilityClientSeries[] = m.clients.map((clientId, c) => {
    const vis: boolean[] = new Array(nSteps)
    const ok: boolean[] = new Array(nSteps)
    const reasons: (string | null)[] = new Array(nSteps)
    for (let t = 0; t < nSteps; t++) {
      const k = t * nClients + c
      vis[t] = visible[k] === 1
      ok[t] = (hops[k] as number) >= 0
      const code = reasonCodes[k] as number
      reasons[t] = code >= 0 ? (m.reason_labels[code] ?? null) : null
    }
    const met = m.client_metrics[clientId]
    return {
      clientId,
      label: labels[clientId],
      visible: vis,
      ok,
      reasons,
      // Манифест отдаёт доли 0..1, компонент ждёт их же — проценты он форматирует сам.
      availPct: met?.avail_pct ?? 0,
      visPct: met?.vis_pct ?? 0,
      maxGapS: met?.max_gap_s ?? 0,
      gapStartCensoredS: met?.gap_start_censored_s ?? 0,
      gapEndCensoredS: met?.gap_end_censored_s ?? 0
    }
  })

  const lla = view(m.buffer, m.layout.sat_lla!) as Float32Array
  const active = view(m.buffer, m.layout.sat_active!) as Uint8Array
  const edgeOffsets = view(m.buffer, m.layout.edge_offsets!) as Uint32Array
  const edgePairs = view(m.buffer, m.layout.edge_pairs!) as Uint16Array
  const routeOffsets = view(m.buffer, m.layout.route_offsets!) as Uint32Array
  const routeNodes = view(m.buffer, m.layout.route_nodes!) as Uint16Array

  const nSat = m.n_sat
  const nodeIds = m.node_ids
  const gatewaySet = new Set(m.gateways)
  const isSat = (nodeIndex: number) => nodeIndex < nSat

  // Период повторения межспутниковой топологии: T/2, где T = 2π√(r³/μ). Доказательство того,
  // что период именно такой, — в docs/MODEL.md (сдвиг на полпериода переводит каждый аппарат
  // в антипод, а расстояния при этом сохраняются).
  const MU = 398600.435507
  const R = 6371
  const orbitR = R + altitudeKm
  const islPeriodS = Math.PI * Math.sqrt(orbitR ** 3 / MU)

  return {
    steps: m.t_s,
    stepS: m.step_s,
    clients: m.clients,
    series,
    islPeriodS,
    hopsAt(index, clientId) {
      const c = m.clients.indexOf(clientId)
      if (c < 0 || index < 0 || index >= nSteps) return null
      const h = hops[index * nClients + c] as number
      return h >= 0 ? h : null
    },

    frameAt(index) {
      const t = Math.min(Math.max(index, 0), nSteps - 1)

      const satellites = new Array(nSat)
      for (let k = 0; k < nSat; k++) {
        const base = (t * nSat + k) * 3
        satellites[k] = {
          id: nodeIds[k]!,
          lat_deg: lla[base]!,
          lon_deg: lla[base + 1]!,
          alt_ratio: lla[base + 2]!,
          active: active[t * nSat + k] === 1
        }
      }

      // CSR: рёбра отсчёта лежат подряд между двумя смещениями — ни поиска, ни фильтрации.
      const islEdges: { a: string, b: string }[] = []
      const groundContacts: { ground_id: string, satellite_id: string }[] = []
      for (let e = edgeOffsets[t]!; e < edgeOffsets[t + 1]!; e++) {
        const a = edgePairs[e * 2]!
        const b = edgePairs[e * 2 + 1]!
        if (isSat(a) && isSat(b)) islEdges.push({ a: nodeIds[a]!, b: nodeIds[b]! })
        else {
          const [g, s] = isSat(a) ? [b, a] : [a, b]
          groundContacts.push({ ground_id: nodeIds[g]!, satellite_id: nodeIds[s]! })
        }
      }

      const routes = m.clients.map((clientId, c) => {
        const k = t * nClients + c
        const from = routeOffsets[k]!
        const to = routeOffsets[k + 1]!
        const path: string[] = []
        for (let i = from; i < to; i++) path.push(nodeIds[routeNodes[i]!]!)
        const h = hops[k] as number
        const code = reasonCodes[k] as number
        const last = path[path.length - 1]
        return {
          client_id: clientId,
          gateway_id: last && gatewaySet.has(last) ? last : null,
          path,
          hops: h >= 0 ? h : null,
          reason: code >= 0 ? (m.reason_labels[code] ?? null) : null
        }
      })

      return { satellites, islEdges, groundContacts, routes }
    },

    trailAt(index, back) {
      const out: { id: string, points: [number, number, number][] }[] = []
      for (let k = 0; k < nSat; k++) {
        if (active[index * nSat + k] !== 1) continue
        const points: [number, number, number][] = []
        for (let t = Math.max(0, index - back); t <= index; t++) {
          const base = (t * nSat + k) * 3
          points.push([lla[base]!, lla[base + 1]!, lla[base + 2]!])
        }
        if (points.length > 1) out.push({ id: nodeIds[k]!, points })
      }
      return out
    }
  }
}
