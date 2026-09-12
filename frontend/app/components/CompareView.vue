<script setup lang="ts">
/**
 * Экран сравнения сохранённых вариантов — DoD п.3 («рекомендации, привязанные к расчётам») и
 * п.4 («экран сравнения с дельтами»). Компонент чисто презентационный, как `GlobeView.vue` и
 * `AvailabilityChart.vue`: сам не ходит в `/ws`, только показывает то, что прислал `compare`
 * (`.claude/rules/protocol.md`, обработчик `backend/app/ws.py:_handle_compare`) и просит
 * пересчёт событием `recompare` — какой composable дёргает `useWs().request('compare', …)` и
 * держит список сохранённых `variant_id`, решает интеграция (DoD п.1, «сохранение варианта и
 * возврат к нему» пока не имеет отдельного хранилища на фронтенде).
 *
 * Форма `result` скопирована из докстринга `backend/app/protocol.py` и тела `_handle_compare`
 * (`backend/app/ws.py:521-599`) дословно, включая snake_case полей — так же, как `GlobeView.vue`
 * не переименовывает `lat_deg`/`alt_ratio` на свой лад. Дельта считается сервером как `b − a`.
 *
 * Две метрики размечены как ОПЦИОНАЛЬНЫЕ прямо в типе `CompareClientMetrics`:
 *   - `hops_median`        — медиана числа переходов маршрута;
 *   - `redundancy_ge2_pct` — доля времени с резервом ≥2 маршрутов (docs/EXTENSIONS.md, §2).
 * На момент написания этого файла `_handle_compare` их не считает (только `vis_pct`/`avail_pct`/
 * `max_gap_s`/censored-перерывы, см. `client_metrics` в `backend/app/ws.py:246-255`), хотя сырьё
 * для обеих уже есть в `core/metrics.py` (`compute()` отдаёт `hops` по каждому отсчёту,
 * `disjoint_paths()` реализован). Раздел «Число переходов» ниже поэтому не выдумывает цифру —
 * строка появляется в ячейке сама, как только бэкенд станет отдавать `hops_median`; колонка
 * «Резерв маршрутов» требуется явно отдельной («если резерв маршрутов посчитан бэкендом, выведи
 * и его отдельной колонкой») и потому не рисуется вовсе, пока `redundancy_ge2_pct` нет ни у
 * одного пункта — это не заглушка, а честное отсутствие ещё не подключённых данных.
 */
import { h, resolveComponent } from 'vue'
import type { TableColumn, TabsItem } from '@nuxt/ui'

const UBadge = resolveComponent('UBadge')
const UProgress = resolveComponent('UProgress')
const UIcon = resolveComponent('UIcon')
const UButton = resolveComponent('UButton')

// ────────────────────────────────── Контракт данных ───────────────────────────────────

/** Вариант в списке выбора — `variant_id` плюс подпись, под которой его сохранил пользователь. */
export interface CompareVariantOption {
  variantId: string
  label: string
}

export interface CompareFieldDiff {
  a: unknown
  b: unknown
}

/** Ровно `client_metrics[c]` из `backend/app/ws.py` — доли 0..1, перерывы в секундах. */
export interface CompareClientMetrics {
  vis_pct: number
  avail_pct: number
  max_gap_s: number
  gap_start_censored_s: number
  gap_end_censored_s: number
  /** Медиана числа переходов маршрута по отсчётам со сквозным путём — опционально, см. докстринг
   *  файла выше: `_handle_compare` сегодня это поле не отдаёт. */
  hops_median?: number | null
  /** Доля отсчётов с ≥2 вершинно-непересекающимися маршрутами «пункт → шлюз» (докстринг файла
   *  выше). Опционально по той же причине. */
  redundancy_ge2_pct?: number | null
}

export interface CompareClientDelta {
  vis_pct: number
  avail_pct: number
  max_gap_s: number
}

export interface CompareClientEntry {
  a: CompareClientMetrics
  b: CompareClientMetrics
  delta: CompareClientDelta
}

export interface ComparePlaneDiff {
  raan_deg?: CompareFieldDiff
  phase_deg?: CompareFieldDiff
}

export interface CompareDesignDiff {
  launch_stage?: CompareFieldDiff
  planes?: Record<string, ComparePlaneDiff>
  planes_only_in_a?: string[]
  planes_only_in_b?: string[]
  n_failures?: CompareFieldDiff
}

/** Payload ответа `compare` целиком — `backend/app/ws.py:_handle_compare`, возврат в конце функции. */
export interface CompareResult {
  variant_id_a: string
  variant_id_b: string
  strategy: string
  /** `false`, когда у вариантов различаются `altitude_km`/`isl_range_km`/`min_elevation_deg`/
   *  `step_s` (docs/PARAMETERS.md) — числа посчитаны честно, но сравнивать «лучше/хуже» нельзя. */
  comparable: boolean
  comparable_warning: string | null
  environment_diff: Record<string, CompareFieldDiff>
  design_diff: CompareDesignDiff
  clients: Record<string, CompareClientEntry>
  clients_only_in_a: string[]
  clients_only_in_b: string[]
}

// ────────────────────────────────────── Props ───────────────────────────────────────

const props = withDefaults(defineProps<{
  /** Список вариантов, доступных для выбора в A/B — сохранённые пользователем `variant_id`. */
  variants: CompareVariantOption[]
  /** Готовый ответ `compare` для ТЕКУЩЕЙ или предыдущей пары вариантов (см. `resultStale` ниже
   *  — если пара сменилась, но пересчёт ещё не запрошен, результат показывается с предупреждением,
   *  а не молча выдаётся за актуальный). */
  result: CompareResult | null
  /** Идёт пересчёт `compare` на сервере — блокирует повторный клик и красит `UTable`/`UEmpty`. */
  loading?: boolean
  /** `environment.target_availability` — планка «доступность ≥ X % по каждому пункту» (CLAUDE.md). */
  targetAvailability?: number
  /** `client_id -> ground_sites[].name` сценария, если у пунктов есть человекочитаемые имена. */
  clientLabels?: Record<string, string>
}>(), {
  loading: false,
  targetAvailability: 0.9,
  clientLabels: () => ({})
})

const emit = defineEmits<{
  /** Пользователь выбрал пару вариантов и просит бэкенд посчитать/обновить `compare`. */
  recompare: []
}>()

const variantIdA = defineModel<string | null>('variantIdA', { default: null })
const variantIdB = defineModel<string | null>('variantIdB', { default: null })

// `USelectMenu` (пустой выбор — `undefined`) и `defineModel` этого компонента (пустой выбор —
// `null`, как и `variantId` в `useScenario.ts`) расходятся в типе «ничего не выбрано» — мостик
// между ними, а не смена сентинела на границе публичного props/emit компонента.
const selectA = computed<string | undefined>({
  get: () => variantIdA.value ?? undefined,
  set: (v) => { variantIdA.value = v ?? null }
})
const selectB = computed<string | undefined>({
  get: () => variantIdB.value ?? undefined,
  set: (v) => { variantIdB.value = v ?? null }
})

function swap(): void {
  const a = variantIdA.value
  variantIdA.value = variantIdB.value
  variantIdB.value = a
}

function findVariant(id: string | null | undefined): CompareVariantOption | null {
  if (!id) return null
  return props.variants.find((v) => v.variantId === id) ?? { variantId: id, label: id }
}

const sameVariantSelected = computed(() =>
  !!variantIdA.value && !!variantIdB.value && variantIdA.value === variantIdB.value
)
const canRequestCompare = computed(() =>
  !!variantIdA.value && !!variantIdB.value && variantIdA.value !== variantIdB.value && !props.loading
)
/** Результат относится к другой паре, чем сейчас выбрана в селектах — типичная ситуация после
 *  того, как пользователь переключил B, но ещё не нажал «Сравнить». */
const resultStale = computed(() =>
  !!props.result &&
  (props.result.variant_id_a !== variantIdA.value || props.result.variant_id_b !== variantIdB.value)
)

// Подписи вариантов результата берутся из САМОГО результата (`result.variant_id_a/b`), а не из
// текущего выбора в селектах — иначе при `resultStale` заголовки таблицы разъедутся с данными.
const resultVariantA = computed(() => (props.result ? findVariant(props.result.variant_id_a) : null))
const resultVariantB = computed(() => (props.result ? findVariant(props.result.variant_id_b) : null))

// ────────────────────────────────── Форматирование ───────────────────────────────────

function formatPercent(fraction: number): string {
  return `${(fraction * 100).toFixed(2)} %`
}
/** Дельта в процентных пунктах, со знаком — единица измерения `docs/PARAMETERS.md` для дельт доступности. */
function formatPctPointsDelta(fraction: number): string {
  const points = fraction * 100
  const sign = points > 0 ? '+' : points < 0 ? '−' : '±'
  return `${sign}${Math.abs(points).toFixed(2)} п.п.`
}
function formatMinutes(seconds: number): string {
  return `${Math.round(seconds / 60)} мин`
}
function formatMinutesDelta(seconds: number): string {
  const minutes = Math.round(seconds / 60)
  const sign = minutes > 0 ? '+' : minutes < 0 ? '−' : '±'
  return `${sign}${Math.abs(minutes)} мин`
}
function formatHops(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}
function formatDiffValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
  }
  if (Array.isArray(v)) return v.length ? v.join(', ') : '—'
  return String(v)
}

/** Ниже этого порога дельту красим нейтрально — не судим о «лучше/хуже» по шуму округления
 *  float32 бинарного пакета. Для перерыва порог грубее (1 мин): реальный шаг сетки сюда не
 *  передаётся (`compare` присылает `environment_diff` только там, где значения РАЗЛИЧАЮТСЯ,
 *  а `step_s` — как раз одно из полей, обязанных совпадать при `comparable: true`), поэтому
 *  минута — намеренное огрубление, а не точная кратность шага конкретного сценария. */
const EPS_AVAIL_FRACTION = 0.0005
const EPS_GAP_SECONDS = 60

type Tone = 'success' | 'error' | 'neutral'

/** `higherIsBetter=false` — для перерыва и его роста: чем длиннее перерыв, тем хуже, даже если
 *  дельта положительна. `comparable=false` гасит цвет в neutral целиком — см. докстринг `CompareResult.comparable`. */
function deltaTone(value: number, eps: number, higherIsBetter: boolean, comparable: boolean): Tone {
  if (!comparable) return 'neutral'
  if (Math.abs(value) < eps) return 'neutral'
  const better = higherIsBetter ? value > 0 : value < 0
  return better ? 'success' : 'error'
}

// ────────────────────────────────── Строки таблицы ───────────────────────────────────

interface ClientRow {
  clientId: string
  label: string
  entry: CompareClientEntry
}

const clientRows = computed<ClientRow[]>(() => {
  if (!props.result) return []
  return Object.keys(props.result.clients)
    .sort()
    .map((clientId) => ({
      clientId,
      label: props.clientLabels[clientId] ?? clientId,
      entry: props.result!.clients[clientId]!
    }))
})

const hasRedundancyData = computed(() =>
  clientRows.value.some((r) => r.entry.a.redundancy_ge2_pct != null || r.entry.b.redundancy_ge2_pct != null)
)
const hasHopsData = computed(() =>
  clientRows.value.some((r) => r.entry.a.hops_median != null || r.entry.b.hops_median != null)
)

// ──────────────────────────── Узкое место: цель — «≥ target по КАЖДОМУ пункту» ────────────────────────────
// CLAUDE.md: «Цель — доступность ≥ 90 % времени по каждому клиенту» — то есть релевантная сводка
// это МИНИМУМ по пунктам, а не среднее: одного проваленного пункта достаточно, чтобы цель не
// считалась достигнутой, средняя доступность это бы скрыла.

interface Bottleneck {
  row: ClientRow
  pct: number
}
function worstClient(pick: (entry: CompareClientEntry) => number): Bottleneck | null {
  return clientRows.value.reduce<Bottleneck | null>((worst, row) => {
    const pct = pick(row.entry)
    return !worst || pct < worst.pct ? { row, pct } : worst
  }, null)
}
const worstA = computed(() => worstClient((e) => e.a.avail_pct))
const worstB = computed(() => worstClient((e) => e.b.avail_pct))

interface SummarySide {
  key: 'a' | 'b'
  label: string
  worst: Bottleneck | null
  goalMet: boolean | null
}
const summarySides = computed<SummarySide[]>(() => [
  {
    key: 'a',
    label: resultVariantA.value?.label ?? props.result?.variant_id_a ?? 'A',
    worst: worstA.value,
    goalMet: worstA.value ? worstA.value.pct >= props.targetAvailability : null
  },
  {
    key: 'b',
    label: resultVariantB.value?.label ?? props.result?.variant_id_b ?? 'B',
    worst: worstB.value,
    goalMet: worstB.value ? worstB.value.pct >= props.targetAvailability : null
  }
])

const onlyInMessage = computed(() => {
  if (!props.result) return ''
  const parts: string[] = []
  if (props.result.clients_only_in_a.length) {
    parts.push(`только в A: ${props.result.clients_only_in_a.join(', ')}`)
  }
  if (props.result.clients_only_in_b.length) {
    parts.push(`только в B: ${props.result.clients_only_in_b.join(', ')}`)
  }
  return `Таблица ниже сравнивает только общие пункты обоих вариантов. Не входят в сравнение — ${parts.join('; ')}.`
})

// ────────────────────────────────── Ячейки таблицы (render-функции) ───────────────────────────────────
// Composite-ячейки собраны через h()/resolveComponent — тот же приём, что в официальном примере
// Nuxt UI для UTable (nuxt-ui/nuxt-ui-full.txt, "With slots"/основной пример): ячейка нужна с
// разными данными (a/b) в одной колонке дважды за строку, обычный `<template #a-cell>` заставил
// бы дублировать разметку целиком под каждый набор данных.

function renderMetricCell(m: CompareClientMetrics, target: number) {
  const okTarget = m.avail_pct >= target
  const children: ReturnType<typeof h>[] = [
    h('div', { class: 'flex items-baseline gap-2' }, [
      h('span', { class: 'text-sm font-semibold text-highlighted tabular-nums' }, formatPercent(m.avail_pct)),
      h(
        UBadge,
        { size: 'xs', variant: 'subtle', color: okTarget ? 'success' : 'error' },
        { default: () => (okTarget ? `≥ ${Math.round(target * 100)} %` : `< ${Math.round(target * 100)} %`) }
      )
    ]),
    h('div', { class: 'relative pt-0.5' }, [
      h(UProgress, {
        modelValue: m.avail_pct * 100,
        max: 100,
        size: 'sm',
        color: okTarget ? 'success' : 'error',
        'aria-label': 'Доступность против цели'
      }),
      h('div', {
        class: 'pointer-events-none absolute inset-y-0.5 w-px bg-[var(--ui-text-dimmed)]',
        style: { left: `${Math.min(Math.max(target * 100, 0), 100)}%` }
      })
    ]),
    h('div', { class: 'text-xs text-muted' }, `перерыв до ${formatMinutes(m.max_gap_s)}`),
    h('div', { class: 'text-xs text-dimmed' }, `видимость ${formatPercent(m.vis_pct)}`)
  ]
  if (m.hops_median != null) {
    children.push(h('div', { class: 'text-xs text-dimmed' }, `переходы (медиана) ${formatHops(m.hops_median)}`))
  }
  return h('div', { class: 'flex flex-col gap-1 min-w-44 py-1' }, children)
}

function renderDeltaCell(entry: CompareClientEntry, comparable: boolean) {
  const rows: Array<{ label: string; text: string; tone: Tone }> = [
    {
      label: 'Доступность',
      text: formatPctPointsDelta(entry.delta.avail_pct),
      tone: deltaTone(entry.delta.avail_pct, EPS_AVAIL_FRACTION, true, comparable)
    },
    {
      label: 'Перерыв',
      text: formatMinutesDelta(entry.delta.max_gap_s),
      tone: deltaTone(entry.delta.max_gap_s, EPS_GAP_SECONDS, false, comparable)
    },
    {
      // Видимость — не целевая метрика (докстринг файла, «видимость ≠ маршрут»), поэтому её
      // дельта всегда нейтральная по цвету: рост видимости не гарантирует рост доступности.
      label: 'Видимость',
      text: formatPctPointsDelta(entry.delta.vis_pct),
      tone: 'neutral' as Tone
    }
  ]
  return h(
    'div',
    { class: 'flex flex-col gap-1 min-w-40 py-1' },
    rows.map((r) =>
      h('div', { class: 'flex items-center justify-between gap-2' }, [
        h('span', { class: 'text-xs text-dimmed' }, r.label),
        h(UBadge, { size: 'xs', variant: 'subtle', color: r.tone }, { default: () => r.text })
      ])
    )
  )
}

function renderRedundancyCell(entry: CompareClientEntry, comparable: boolean) {
  const a = entry.a.redundancy_ge2_pct
  const b = entry.b.redundancy_ge2_pct
  if (a == null && b == null) return h('span', { class: 'text-xs text-dimmed' }, '—')
  const delta = a != null && b != null ? b - a : null
  const rows = [
    h('div', { class: 'flex items-center justify-between gap-2' }, [
      h('span', { class: 'text-xs text-dimmed' }, 'A'),
      h('span', { class: 'text-xs tabular-nums' }, a != null ? formatPercent(a) : '—')
    ]),
    h('div', { class: 'flex items-center justify-between gap-2' }, [
      h('span', { class: 'text-xs text-dimmed' }, 'B'),
      h('span', { class: 'text-xs tabular-nums' }, b != null ? formatPercent(b) : '—')
    ])
  ]
  if (delta != null) {
    rows.push(
      h('div', { class: 'flex items-center justify-between gap-2' }, [
        h('span', { class: 'text-xs text-dimmed' }, 'Δ'),
        h(
          UBadge,
          { size: 'xs', variant: 'subtle', color: deltaTone(delta, EPS_AVAIL_FRACTION, true, comparable) },
          { default: () => formatPctPointsDelta(delta) }
        )
      ])
    )
  }
  return h('div', { class: 'flex flex-col gap-1 min-w-32 py-1' }, rows)
}

function sortableHeader(label: string, column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void }) {
  const sorted = column.getIsSorted()
  return h(UButton, {
    color: 'neutral',
    variant: 'ghost',
    label,
    size: 'xs',
    icon: sorted ? (sorted === 'asc' ? 'i-lucide-arrow-up-narrow-wide' : 'i-lucide-arrow-down-wide-narrow') : 'i-lucide-arrow-up-down',
    class: '-mx-2.5',
    onClick: () => column.toggleSorting(sorted === 'asc')
  })
}

const columns = computed<TableColumn<ClientRow>[]>(() => {
  const target = props.targetAvailability
  const comparable = props.result?.comparable ?? true
  const labelA = resultVariantA.value?.label ?? props.result?.variant_id_a ?? 'A'
  const labelB = resultVariantB.value?.label ?? props.result?.variant_id_b ?? 'B'

  const cols: TableColumn<ClientRow>[] = [
    {
      id: 'client',
      header: 'Пункт',
      accessorFn: (r) => r.label,
      cell: ({ row }) => h('span', { class: 'font-medium text-highlighted' }, row.original.label)
    },
    {
      id: 'a',
      header: () => `A — ${labelA}`,
      accessorFn: (r) => r.entry.a.avail_pct,
      cell: ({ row }) => renderMetricCell(row.original.entry.a, target)
    },
    {
      id: 'b',
      header: () => `B — ${labelB}`,
      accessorFn: (r) => r.entry.b.avail_pct,
      cell: ({ row }) => renderMetricCell(row.original.entry.b, target)
    },
    {
      id: 'delta',
      header: ({ column }) => sortableHeader('Δ (B − A)', column),
      accessorFn: (r) => r.entry.delta.avail_pct,
      cell: ({ row }) => renderDeltaCell(row.original.entry, comparable)
    }
  ]

  // Отдельной колонкой — только когда бэкенд реально прислал резерв маршрутов хотя бы для
  // одного пункта; иначе колонка не рисуется вовсе (см. докстринг файла).
  if (hasRedundancyData.value) {
    cols.push({
      id: 'redundancy',
      header: 'Резерв маршрутов (≥2 путей)',
      accessorFn: (r) => r.entry.b.redundancy_ge2_pct ?? r.entry.a.redundancy_ge2_pct ?? 0,
      cell: ({ row }) => renderRedundancyCell(row.original.entry, comparable)
    })
  }

  return cols
})

// ────────────────────────────────── Изменённые параметры ───────────────────────────────────

const ENV_LABELS: Record<string, string> = {
  altitude_km: 'Высота орбиты, км',
  inclination_deg: 'Наклонение, °',
  earth_angle0_deg: 'Начальный поворот Земли, °',
  horizon_s: 'Горизонт расчёта, с',
  step_s: 'Шаг сетки, с',
  min_elevation_deg: 'Мин. угол возвышения, °',
  isl_range_km: 'Дальность ISL, км',
  target_availability: 'Целевая доступность'
}
/** Ровно `_COMPARABLE_FIELDS` из `backend/app/ws.py` — различие в них и делает `comparable: false`. */
const COMPARABILITY_FIELDS = new Set(['altitude_km', 'isl_range_km', 'min_elevation_deg', 'step_s'])

interface DiffRow {
  key: string
  label: string
  a: string
  b: string
  /** Поле входит в `_COMPARABLE_FIELDS` — различие в нём объясняет `comparable_warning`. */
  affectsComparability: boolean
}

const diffRows = computed<DiffRow[]>(() => {
  if (!props.result) return []
  const rows: DiffRow[] = []

  for (const [key, diff] of Object.entries(props.result.environment_diff)) {
    rows.push({
      key: `env.${key}`,
      label: ENV_LABELS[key] ?? key,
      a: formatDiffValue(diff.a),
      b: formatDiffValue(diff.b),
      affectsComparability: COMPARABILITY_FIELDS.has(key)
    })
  }

  const dd = props.result.design_diff
  if (dd.launch_stage) {
    rows.push({
      key: 'design.launch_stage',
      label: 'Очередь запуска',
      a: formatDiffValue(dd.launch_stage.a),
      b: formatDiffValue(dd.launch_stage.b),
      affectsComparability: false
    })
  }
  if (dd.planes) {
    for (const [planeId, changes] of Object.entries(dd.planes)) {
      if (changes.raan_deg) {
        rows.push({
          key: `design.planes.${planeId}.raan_deg`,
          label: `RAAN плоскости ${planeId}, °`,
          a: formatDiffValue(changes.raan_deg.a),
          b: formatDiffValue(changes.raan_deg.b),
          affectsComparability: false
        })
      }
      if (changes.phase_deg) {
        rows.push({
          key: `design.planes.${planeId}.phase_deg`,
          label: `Фаза плоскости ${planeId}, °`,
          a: formatDiffValue(changes.phase_deg.a),
          b: formatDiffValue(changes.phase_deg.b),
          affectsComparability: false
        })
      }
    }
  }
  if (dd.planes_only_in_a?.length) {
    rows.push({
      key: 'design.planes_only_in_a',
      label: 'Плоскости только в A',
      a: dd.planes_only_in_a.join(', '),
      b: '—',
      affectsComparability: false
    })
  }
  if (dd.planes_only_in_b?.length) {
    rows.push({
      key: 'design.planes_only_in_b',
      label: 'Плоскости только в B',
      a: '—',
      b: dd.planes_only_in_b.join(', '),
      affectsComparability: false
    })
  }
  if (dd.n_failures) {
    rows.push({
      key: 'design.n_failures',
      label: 'Периодов отказа',
      a: formatDiffValue(dd.n_failures.a),
      b: formatDiffValue(dd.n_failures.b),
      affectsComparability: false
    })
  }

  return rows
})

const diffColumns = computed<TableColumn<DiffRow>[]>(() => [
  {
    id: 'label',
    header: 'Параметр',
    accessorFn: (r) => r.label,
    cell: ({ row }) =>
      h('span', { class: 'flex items-center gap-1.5' }, [
        row.original.affectsComparability
          ? h(UIcon, { name: 'i-lucide-triangle-alert', class: 'size-3.5 text-warning shrink-0' })
          : null,
        h('span', row.original.label)
      ])
  },
  { id: 'a', header: () => `A — ${resultVariantA.value?.label ?? 'A'}`, accessorFn: (r) => r.a },
  { id: 'b', header: () => `B — ${resultVariantB.value?.label ?? 'B'}`, accessorFn: (r) => r.b }
])

const tabs: TabsItem[] = [
  { label: 'Показатели по пунктам', icon: 'i-lucide-table-2', slot: 'metrics' },
  { label: 'Изменённые параметры', icon: 'i-lucide-list-tree', slot: 'params' }
]
</script>

<template>
  <div class="flex flex-col gap-5">
    <!-- Выбор пары вариантов -->
    <div class="flex flex-wrap items-end gap-2">
      <div class="flex flex-col gap-1 min-w-52">
        <label class="text-xs font-medium text-muted">Вариант A</label>
        <USelectMenu
          v-model="selectA"
          :items="variants"
          value-key="variantId"
          label-key="label"
          icon="i-lucide-git-branch"
          placeholder="Выберите вариант"
          :disabled="loading"
        />
      </div>

      <UTooltip text="Поменять местами">
        <UButton
          icon="i-lucide-arrow-left-right"
          color="neutral"
          variant="outline"
          :disabled="!variantIdA && !variantIdB"
          aria-label="Поменять местами"
          @click="swap"
        />
      </UTooltip>

      <div class="flex flex-col gap-1 min-w-52">
        <label class="text-xs font-medium text-muted">Вариант B</label>
        <USelectMenu
          v-model="selectB"
          :items="variants"
          value-key="variantId"
          label-key="label"
          icon="i-lucide-git-branch"
          placeholder="Выберите вариант"
          :disabled="loading"
        />
      </div>

      <UButton
        label="Сравнить"
        icon="i-lucide-scale"
        :disabled="!canRequestCompare"
        :loading="loading"
        @click="emit('recompare')"
      />
    </div>

    <!-- Основное содержимое: результат в приоритете, даже устаревший (сопровождается баннером) -->
    <template v-if="result">
      <UAlert
        v-if="resultStale"
        color="warning"
        variant="subtle"
        icon="i-lucide-refresh-cw"
        title="Показан результат для другой пары вариантов"
        description="Выбор в A/B изменился — нажмите «Сравнить», чтобы пересчитать для текущей пары."
      />

      <UAlert
        v-if="!result.comparable"
        color="warning"
        variant="subtle"
        icon="i-lucide-triangle-alert"
        title="Сравнение некорректно"
        :description="result.comparable_warning ?? undefined"
      />

      <UAlert
        v-if="result.clients_only_in_a.length || result.clients_only_in_b.length"
        color="neutral"
        variant="subtle"
        icon="i-lucide-info"
        title="Наборы пунктов различаются"
        :description="onlyInMessage"
      />

      <!-- Цель: доступность ≥ target ПО КАЖДОМУ пункту (CLAUDE.md) — сводка по узкому месту -->
      <div v-if="clientRows.length" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <UCard v-for="side in summarySides" :key="side.key" variant="subtle">
          <template #header>
            <div class="flex items-center justify-between gap-2">
              <span class="font-semibold text-highlighted truncate">{{ side.key.toUpperCase() }} — {{ side.label }}</span>
              <UBadge
                v-if="side.goalMet !== null"
                :color="side.goalMet ? 'success' : 'error'"
                variant="subtle"
                :icon="side.goalMet ? 'i-lucide-circle-check' : 'i-lucide-circle-alert'"
                :label="side.goalMet ? `цель ${Math.round(targetAvailability * 100)}% достигнута везде` : 'цель достигнута не везде'"
              />
            </div>
          </template>
          <div class="flex flex-col gap-2">
            <div class="flex items-center justify-between text-sm">
              <span class="text-muted">узкое место: {{ side.worst?.row.label ?? '—' }}</span>
              <span class="font-semibold tabular-nums">{{ side.worst ? formatPercent(side.worst.pct) : '—' }}</span>
            </div>
            <UProgress
              :model-value="(side.worst?.pct ?? 0) * 100"
              :max="100"
              size="sm"
              :color="side.goalMet ? 'success' : 'error'"
              aria-label="Доступность худшего пункта против цели"
            />
          </div>
        </UCard>
      </div>

      <!-- Показатели по пунктам / изменённые параметры — раздельные вкладки одного экрана
           сравнения (frontend.md, строка «Сравнение вариантов»: UTabs + UTable вместе). -->
      <UTabs :items="tabs" :ui="{ content: 'pt-4' }">
        <template #metrics>
          <div v-if="clientRows.length" class="flex flex-col gap-2">
            <div class="overflow-x-auto">
              <UTable
                :data="clientRows"
                :columns="columns"
                :loading="loading"
                :get-row-id="(row: ClientRow) => row.clientId"
                :initial-state="{ sorting: [{ id: 'delta', desc: false }] }"
                class="min-w-[640px]"
              />
            </div>
            <p class="text-xs text-dimmed">
              Δ = B − A. Для перерыва рост дельты — это ухудшение, цвет бейджа учитывает это отдельно от знака.
            </p>
            <UAlert
              v-if="!hasHopsData"
              color="neutral"
              variant="subtle"
              icon="i-lucide-route"
              title="Число переходов подключится само"
              description="core.metrics.compute() уже считает число переходов на каждом отсчёте — как только client_metrics в /ws начнёт отдавать сводную статистику по ним, строка «переходы» появится в ячейках выше без правок этого экрана."
            />
          </div>

          <UEmpty
            v-else
            icon="i-lucide-users"
            title="Нет общих клиентских пунктов"
            description="У выбранных вариантов не совпадает ни один client_id — сравнивать показатели по пунктам нечего."
          />
        </template>

        <template #params>
          <div v-if="diffRows.length" class="overflow-x-auto">
            <UTable :data="diffRows" :columns="diffColumns" :get-row-id="(row: DiffRow) => row.key" class="min-w-[420px]" />
          </div>
          <UAlert
            v-else
            color="neutral"
            variant="subtle"
            icon="i-lucide-info"
            title="Параметры конфигурации совпадают"
            description="Разница в показателях на вкладке «Показатели по пунктам» вызвана не изменением параметров (например, разными интервалами failures на одном и том же design)."
          />
        </template>
      </UTabs>
    </template>

    <UAlert
      v-else-if="variants.length < 2"
      color="neutral"
      variant="subtle"
      icon="i-lucide-info"
      title="Нужно минимум два сохранённых варианта"
      description="Сохраните вариант конфигурации на экране редактирования группировки, затем вернитесь сюда, чтобы сравнить его с другим."
    />

    <UAlert
      v-else-if="sameVariantSelected"
      color="neutral"
      variant="subtle"
      icon="i-lucide-info"
      title="Выбран один и тот же вариант дважды"
      description="Дельты будут нулевыми — выберите для A и B два разных сохранённых варианта."
    />

    <UEmpty
      v-else
      icon="i-lucide-scale"
      :loading="loading"
      title="Выберите два варианта и нажмите «Сравнить»"
      description="Показатели по пунктам, резерв маршрутов (если посчитан) и изменённые параметры появятся здесь."
    />
  </div>
</template>
