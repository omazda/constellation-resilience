<script setup lang="ts">
/**
 * Диаграмма доступности и перерывов по каждому клиентскому пункту. Готового компонента в
 * Nuxt UI нет — полоса каждого пункта рисуется своим SVG на токенах
 * темы (`var(--ui-success)`/`--ui-warning`/`--ui-error`/`--ui-bg`), а не самописной разметкой
 * поверх чужой палитры.
 *
 * Три состояния отсчёта на полосе (а не два) — это и есть главный тезис кейса «видимость ≠
 * маршрут» (docs/README.md, вывод №2), выраженный прямо в диаграмме:
 *   есть маршрут          — `var(--ui-success)`
 *   спутник виден, маршрута нет (разрыв ISL/шлюза) — `var(--ui-warning)`
 *   спутник не виден совсем     — `var(--ui-error)`
 * Перерыв, упирающийся в t=0 или в конец горизонта, усечён (docs/PARAMETERS.md, «цензурированные
 * перерывы») — такие отрезки дополнительно перекрыты диагональной штриховкой, чтобы не читались
 * как измеренная длина перерыва.
 *
 * Клик по полосе — `emit('seek', index)`, индекс в той же сетке `tSeconds`, что и `v-model`
 * `TimelineBar.vue`: страница интеграции сводит оба компонента к одному состоянию индекса.
 *
 * Наведение — общий `UTooltip` с виртуальным `reference`, следующим за курсором (официальный
 * рецепт Nuxt UI «with following cursor»): использует готовый компонент, а не самописный
 * тултип, при этом не плодит по инстансу `UTooltip` на каждый из сотен отрезков перерывов.
 */

export type AvailabilitySegmentState = 'ok' | 'no_route' | 'no_visibility'

export interface AvailabilityClientSeries {
  /** Идентификатор клиентского пункта — `ground_sites[].id` сценария. */
  clientId: string
  /** Подпись для отображения, если должна отличаться от `clientId` (по умолчанию — он же). */
  label?: string
  /** Видимость ≥1 активного спутника по сетке `tSeconds` — `core.metrics.compute().clients[c].visible`. */
  visible: boolean[]
  /** Есть ли сквозной маршрут по сетке `tSeconds` — `core.metrics.compute().clients[c].ok`. */
  ok: boolean[]
  /** Точная причина отсутствия маршрута на отсчёте (`reason_codes`/`reason_labels` бинарного
   *  пакета `compute`, `docs/PROTOCOL.md`) — «нет видимого спутника» / «разрыв ISL» /
   *  «нет контакта со шлюзом» / «шлюз недоступен». Необязательно: без неё подсказка показывает
   *  укрупнённую причину по `visible[i]`. Длина массива — как у `ok`. */
  reasons?: (string | null | undefined)[]
  /** Доля отсчётов со сквозным маршрутом, 0..1 — `.avail_pct`. */
  availPct: number
  /** Доля отсчётов с видимостью, 0..1 — `.vis_pct`. */
  visPct: number
  /** Максимальный перерыв, секунды (без учёта усечённых по краям) — `.max_gap_s`. */
  maxGapS: number
  /** Перерыв у t=0, если усечён (0 — t=0 доступен) — `.gap_start_censored_s`. */
  gapStartCensoredS: number
  /** Перерыв у конца горизонта, если усечён (0 — конец горизонта доступен) — `.gap_end_censored_s`. */
  gapEndCensoredS: number
}

interface Segment {
  startIdx: number
  endIdx: number // не включая — как правая граница сетки в docs/PARAMETERS.md
  state: AvailabilitySegmentState
  censoredStart: boolean
  censoredEnd: boolean
}

const props = withDefaults(defineProps<{
  /** Сетка отсчётов в секундах — та же, что во `v-model` `TimelineBar.vue`. */
  tSeconds: number[]
  clients: AvailabilityClientSeries[]
  /** Индекс текущего отсчёта — рисуется вертикальный маркер, синхронный со шкалой. */
  currentIndex?: number | null
  /** Цель по доступности, 0..1 — `environment.target_availability`, подсвечивает бейдж. */
  targetAvailability?: number
}>(), {
  currentIndex: null,
  targetAvailability: 0.9
})

const emit = defineEmits<{ seek: [index: number] }>()

const n = computed(() => props.tSeconds.length)
const stepS = computed(() => (n.value > 1 ? props.tSeconds[1]! - props.tSeconds[0]! : 1))
/** Правая граница горизонта — сетка её не включает (docs/PARAMETERS.md), но полоса должна дорисовываться
 *  до неё, иначе последний отсчёт визуально теряет свою долю ширины. */
const domainEndS = computed(() => (n.value > 0 ? props.tSeconds[n.value - 1]! + stepS.value : 0))

function clamp(x: number, lo: number, hi: number): number {
  return Math.min(Math.max(x, lo), hi)
}

// ────────────────────────────────── Форматирование ───────────────────────────────────

function formatOffset(totalSeconds: number): string {
  const abs = Math.max(0, Math.round(totalSeconds))
  const days = Math.floor(abs / 86400)
  const hours = Math.floor((abs % 86400) / 3600)
  const minutes = Math.floor((abs % 3600) / 60)
  const seconds = abs % 60
  const hh = String(hours).padStart(2, '0')
  const mm = String(minutes).padStart(2, '0')
  const ss = String(seconds).padStart(2, '0')
  return days > 0 ? `${days} д ${hh}:${mm}:${ss}` : `${hh}:${mm}:${ss}`
}
/** Минуты, не часы — так показаны перерывы во всех числах исследования (docs/PARAMETERS.md,
 *  docs/README.md) и в `tools/golden.py`, свой формат завёл бы расхождение с эталонными числами. */
function formatMinutes(totalSeconds: number): string {
  return `${Math.round(totalSeconds / 60)} мин`
}
function formatPercent(fraction: number): string {
  return `${(fraction * 100).toFixed(2)} %`
}

// ────────────────────────────────── Отрезки полосы ───────────────────────────────────

function stateAt(series: AvailabilityClientSeries, i: number): AvailabilitySegmentState {
  if (series.ok[i]) return 'ok'
  return series.visible[i] ? 'no_route' : 'no_visibility'
}

function buildSegments(series: AvailabilityClientSeries): Segment[] {
  const len = series.ok.length
  if (len === 0) return []
  const segments: Segment[] = []
  let curState = stateAt(series, 0)
  let curStart = 0
  for (let i = 1; i <= len; i++) {
    const st = i < len ? stateAt(series, i) : null
    if (st !== curState) {
      segments.push({
        startIdx: curStart,
        endIdx: i,
        state: curState,
        censoredStart: curStart === 0 && curState !== 'ok' && series.gapStartCensoredS > 0,
        censoredEnd: i === len && curState !== 'ok' && series.gapEndCensoredS > 0
      })
      curStart = i
      curState = st as AvailabilitySegmentState
    }
  }
  return segments
}

const STATE_COLOR: Record<AvailabilitySegmentState, string> = {
  ok: 'var(--ui-success)',
  no_route: 'var(--ui-warning)',
  no_visibility: 'var(--ui-error)'
}
const STATE_LABEL: Record<AvailabilitySegmentState, string> = {
  ok: 'маршрут есть',
  no_route: 'спутник виден, маршрута нет',
  no_visibility: 'спутник не виден'
}

interface Row {
  series: AvailabilityClientSeries
  segments: Segment[]
}
const rows = computed<Row[]>(() => props.clients.map((series) => ({ series, segments: buildSegments(series) })))
const rowsById = computed(() => new Map(rows.value.map((r) => [r.series.clientId, r])))

// ────────────────────────────────── Геометрия/измерение ───────────────────────────────────

const ROW_HEIGHT = 24 // px, высота полосы одного пункта в SVG
const bandEl = ref<HTMLElement | null>(null)
const bandWidth = ref(400) // разумное значение до первого измерения ResizeObserver
let resizeObserver: ResizeObserver | null = null

onMounted(() => {
  if (!bandEl.value) return
  resizeObserver = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect.width
    if (w && w > 0) bandWidth.value = w
  })
  resizeObserver.observe(bandEl.value)
})
onBeforeUnmount(() => resizeObserver?.disconnect())

function segX(seg: Segment): number {
  return (seg.startIdx / n.value) * bandWidth.value
}
function segW(seg: Segment): number {
  return ((seg.endIdx - seg.startIdx) / n.value) * bandWidth.value
}

const playheadPct = computed(() => {
  if (props.currentIndex === null || props.currentIndex === undefined || n.value === 0) return null
  return (clamp(props.currentIndex, 0, n.value - 1) / n.value) * 100
})

// Уникальный суффикс id — на экране сравнения вариантов диаграмма может стоять рядом сама с
// собой (два variant_id), а SVG id глобальны на весь документ.
const uid = useId()
const clipId = `avail-clip-${uid}`
const hatchId = `avail-hatch-${uid}`

// Шесть подписей оси времени, последняя — по правой границе горизонта (а не по последнему
// отсчёту сетки, который её не включает).
const AXIS_TICKS = 6
const axisTicks = computed(() => {
  if (n.value === 0) return []
  return Array.from({ length: AXIS_TICKS }, (_, k) => {
    const pct = (k / (AXIS_TICKS - 1)) * 100
    const seconds = k === AXIS_TICKS - 1 ? domainEndS.value : props.tSeconds[Math.round((k / (AXIS_TICKS - 1)) * (n.value - 1))]!
    return { pct, label: formatOffset(seconds) }
  })
})

// ────────────────────────────────── Наведение и клик ───────────────────────────────────

function indexFromClientX(el: HTMLElement, clientX: number): number {
  const rect = el.getBoundingClientRect()
  if (rect.width <= 0 || n.value === 0) return 0
  const frac = clamp((clientX - rect.left) / rect.width, 0, 1)
  return clamp(Math.floor(frac * n.value), 0, n.value - 1)
}

function segmentAt(clientId: string, idx: number): Segment | null {
  const row = rowsById.value.get(clientId)
  if (!row) return null
  return row.segments.find((s) => idx >= s.startIdx && idx < s.endIdx) ?? null
}

const tooltipOpen = ref(false)
const tooltipText = ref('')
const pointerPos = ref({ x: 0, y: 0 })
const tooltipReference = computed(() => ({
  getBoundingClientRect: () => ({
    width: 0,
    height: 0,
    left: pointerPos.value.x,
    right: pointerPos.value.x,
    top: pointerPos.value.y,
    bottom: pointerPos.value.y,
    x: pointerPos.value.x,
    y: pointerPos.value.y,
    toJSON() {}
  } as DOMRect)
}))

function buildTooltipText(series: AvailabilityClientSeries, idx: number): string {
  const seg = segmentAt(series.clientId, idx)
  const label = series.label ?? series.clientId
  if (!seg) return label
  const stateLabel = STATE_LABEL[seg.state]
  const startS = props.tSeconds[seg.startIdx]!
  const endS = seg.endIdx < n.value ? props.tSeconds[seg.endIdx]! : domainEndS.value
  const parts = [label, stateLabel]
  if (seg.state !== 'ok') {
    const reason = series.reasons?.[idx]
    if (reason) parts.push(reason)
    if (seg.censoredStart || seg.censoredEnd) parts.push('перерыв усечён горизонтом, длиннее показанного')
  }
  parts.push(`${formatOffset(startS)}–${formatOffset(endS)} (${formatMinutes(endS - startS)})`)
  return parts.join(' · ')
}

function onBandPointerMove(event: PointerEvent, series: AvailabilityClientSeries): void {
  const el = event.currentTarget as HTMLElement
  const idx = indexFromClientX(el, event.clientX)
  pointerPos.value = { x: event.clientX, y: event.clientY }
  tooltipText.value = buildTooltipText(series, idx)
  tooltipOpen.value = true
}
function onBandPointerLeave(): void {
  tooltipOpen.value = false
}
function onBandClick(event: MouseEvent): void {
  const el = event.currentTarget as HTMLElement
  emit('seek', indexFromClientX(el, event.clientX))
}
</script>

<template>
  <UEmpty
    v-if="n === 0 || clients.length === 0"
    icon="i-lucide-activity"
    title="Нет данных для диаграммы"
    description="Запустите расчёт (compute) — здесь появятся интервалы связи и перерывы по каждому клиентскому пункту."
  />

  <div v-else class="flex flex-col gap-3">
    <!-- Скрытые общие ресурсы SVG: клип углов полосы и штриховка усечённых перерывов, один
         набор на все строки (id глобальны на документ — как и у слоёв globe.gl,
         ресурсы SVG здесь общие). -->
    <svg width="0" height="0" class="absolute" aria-hidden="true">
      <defs>
        <clipPath :id="clipId">
          <rect x="0" y="0" :width="bandWidth" :height="ROW_HEIGHT" rx="4" />
        </clipPath>
        <pattern :id="hatchId" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="8" height="8" fill="transparent" />
          <line x1="0" y1="0" x2="0" y2="8" stroke="var(--ui-bg)" stroke-width="3" stroke-opacity="0.55" />
        </pattern>
      </defs>
    </svg>

    <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
      <span class="flex items-center gap-1.5">
        <span class="size-2.5 rounded-full shrink-0" :style="{ background: STATE_COLOR.ok }" />
        Маршрут есть
      </span>
      <span class="flex items-center gap-1.5">
        <span class="size-2.5 rounded-full shrink-0" :style="{ background: STATE_COLOR.no_route }" />
        Спутник виден, маршрута нет
      </span>
      <span class="flex items-center gap-1.5">
        <span class="size-2.5 rounded-full shrink-0" :style="{ background: STATE_COLOR.no_visibility }" />
        Спутник не виден
      </span>
      <span class="flex items-center gap-1.5">
        <span
          class="size-2.5 rounded-full shrink-0"
          style="background-color: var(--ui-error); background-image: repeating-linear-gradient(45deg, transparent 0 2px, var(--ui-bg) 2px 3px);"
        />
        Перерыв усечён горизонтом
      </span>
    </div>

    <UTooltip
      :open="tooltipOpen"
      :reference="tooltipReference"
      :text="tooltipText"
      :content="{ side: 'top', sideOffset: 14, updatePositionStrategy: 'always' }"
      :delay-duration="0"
    >
      <div class="flex flex-col">
        <!-- Ось времени -->
        <div class="flex">
          <div class="w-24 sm:w-40 md:w-48 shrink-0" />
          <div ref="bandEl" class="relative flex-1 min-w-0 h-5">
            <span
              v-for="tick in axisTicks"
              :key="tick.pct"
              class="absolute top-0 -translate-x-1/2 text-[10px] text-dimmed whitespace-nowrap"
              :style="{ left: tick.pct + '%' }"
            >{{ tick.label }}</span>
          </div>
        </div>

        <!-- Строки пунктов -->
        <div
          v-for="row in rows"
          :key="row.series.clientId"
          class="flex items-center gap-2 py-1.5 border-b border-muted last:border-b-0"
        >
          <div class="w-24 sm:w-40 md:w-48 shrink-0 min-w-0">
            <div class="flex items-center gap-1.5 min-w-0">
              <span class="text-sm font-medium text-highlighted truncate">
                {{ row.series.label ?? row.series.clientId }}
              </span>
              <UBadge
                :label="formatPercent(row.series.availPct)"
                :color="row.series.availPct >= targetAvailability ? 'success' : 'error'"
                variant="subtle"
                size="sm"
              />
            </div>
            <div class="hidden sm:block text-xs text-muted truncate">
              видимость {{ formatPercent(row.series.visPct) }} · перерыв до {{ formatMinutes(row.series.maxGapS) }}
            </div>
          </div>

          <div
            class="relative flex-1 min-w-0 cursor-pointer"
            :style="{ height: ROW_HEIGHT + 'px' }"
            role="img"
            :aria-label="`${row.series.label ?? row.series.clientId}: доступность ${formatPercent(row.series.availPct)}, максимальный перерыв ${formatMinutes(row.series.maxGapS)}`"
            @pointermove="onBandPointerMove($event, row.series)"
            @pointerleave="onBandPointerLeave"
            @click="onBandClick"
          >
            <svg
              :viewBox="`0 0 ${bandWidth} ${ROW_HEIGHT}`"
              :width="bandWidth"
              :height="ROW_HEIGHT"
              preserveAspectRatio="none"
              class="block w-full h-full"
            >
              <rect x="0" y="0" :width="bandWidth" :height="ROW_HEIGHT" rx="4" fill="var(--ui-bg-muted)" />
              <g :clip-path="`url(#${clipId})`">
                <rect
                  v-for="(seg, i) in row.segments"
                  :key="i"
                  :x="segX(seg)"
                  y="0"
                  :width="segW(seg)"
                  :height="ROW_HEIGHT"
                  :fill="STATE_COLOR[seg.state]"
                />
                <rect
                  v-for="(seg, i) in row.segments.filter((s) => s.censoredStart || s.censoredEnd)"
                  :key="'c' + i"
                  :x="segX(seg)"
                  y="0"
                  :width="segW(seg)"
                  :height="ROW_HEIGHT"
                  :fill="`url(#${hatchId})`"
                />
              </g>
            </svg>
            <div
              v-if="playheadPct !== null"
              class="pointer-events-none absolute inset-y-0 w-px"
              style="background: var(--ui-primary);"
              :style="{ left: playheadPct + '%' }"
            />
          </div>
        </div>
      </div>
    </UTooltip>
  </div>
</template>
