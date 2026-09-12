<script setup lang="ts">
/**
 * Полоса суток — сутки целиком, а не ползунок.
 *
 * Обычный таймлайн заставляет искать перерывы перетаскиванием: пользователь видит один момент
 * и должен угадать, где искать остальные. Здесь весь горизонт показан сразу — по строке на
 * наземный пункт, по столбцу на отсчёт, — и перерывы видны как разрывы, без поиска.
 *
 * Наведение мыши мгновенно переводит глобус в этот момент. Это возможно только потому, что
 * пакет `compute` привозит все отсчёты разом (docs/PROTOCOL.md): ни одного запроса к серверу
 * при перемотке не происходит, поэтому картинка идёт за курсором с частотой кадров экрана.
 *
 * Насечки сверху — период повторения межспутниковой топологии, половина периода обращения
 * (docs/MODEL.md). На сутках видно, что структура сети повторяется примерно каждые 48 минут:
 * свойство задачи, а не украшение.
 */
import type { AvailabilityClientSeries } from './AvailabilityChart.vue'

const props = defineProps<{
  tSeconds: number[]
  clients: AvailabilityClientSeries[]
  /** Период повторения топологии, секунды — насечки сверху. */
  periodS?: number
  selectedClientId?: string | null
}>()

/** Закреплённый отсчёт: к нему возвращаемся, когда курсор уходит с полосы. */
const pinned = defineModel<number>({ default: 0 })
const emit = defineEmits<{ preview: [index: number] }>()

const canvasRef = ref<HTMLCanvasElement | null>(null)
const hoverIndex = ref<number | null>(null)
const wrapRef = ref<HTMLDivElement | null>(null)

const ROW = 16
const GAP = 3
const TOP = 14

const height = computed(() => TOP + props.clients.length * (ROW + GAP))

function cssVar(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function draw(): void {
  const canvas = canvasRef.value
  const wrap = wrapRef.value
  if (!canvas || !wrap) return

  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = Math.max(1, Math.round(wrap.clientWidth))
  const h = height.value
  canvas.width = Math.round(w * dpr)
  canvas.height = Math.round(h * dpr)
  canvas.style.width = `${w}px`
  canvas.style.height = `${h}px`

  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)

  const n = props.tSeconds.length
  if (!n) return
  const colW = w / n

  const ok = cssVar('--color-ion-400', '#38f0d0')
  const partial = cssVar('--color-uv-500', '#8b5cf6')
  const dark = cssVar('--color-graphite-800', '#221b33')
  const tick = cssVar('--color-uv-300', '#b692ff')

  // Строки состояний. Три состояния вместо двух: «спутник виден, но маршрута нет» — это
  // разрыв межспутниковой сети, а не отсутствие покрытия, и лечится он по-разному.
  props.clients.forEach((series, row) => {
    const y = TOP + row * (ROW + GAP)
    for (let i = 0; i < n; i++) {
      const state = series.ok[i] ? ok : series.visible[i] ? partial : dark
      ctx.fillStyle = state
      ctx.globalAlpha = series.ok[i] ? 0.95 : series.visible[i] ? 0.5 : 0.9
      ctx.fillRect(i * colW, y, Math.max(colW, 0.7), ROW)
    }
    ctx.globalAlpha = 1
    if (props.selectedClientId === series.clientId) {
      ctx.strokeStyle = cssVar('--color-uv-300', '#b692ff')
      ctx.lineWidth = 1
      ctx.strokeRect(0.5, y - 0.5, w - 1, ROW + 1)
    }
  })

  // Насечки периода повторения топологии.
  if (props.periodS && props.periodS > 0) {
    const total = props.tSeconds[n - 1]! + (props.tSeconds[1]! - props.tSeconds[0]!)
    ctx.fillStyle = tick
    ctx.globalAlpha = 0.35
    for (let t = props.periodS; t < total; t += props.periodS) {
      ctx.fillRect((t / total) * w, 0, 1, 6)
    }
    ctx.globalAlpha = 1
  }

  // Курсор и закреплённый отсчёт.
  const marks: [number, number, string][] = [[pinned.value, 1, cssVar('--color-uv-300', '#b692ff')]]
  if (hoverIndex.value != null) marks.push([hoverIndex.value, 1.5, '#ffffff'])
  for (const [idx, lw, color] of marks) {
    const x = (idx + 0.5) * colW
    ctx.strokeStyle = color
    ctx.lineWidth = lw
    ctx.beginPath()
    ctx.moveTo(x, 0)
    ctx.lineTo(x, h)
    ctx.stroke()
  }
}

function indexFromEvent(e: PointerEvent): number {
  const wrap = wrapRef.value
  if (!wrap) return 0
  const rect = wrap.getBoundingClientRect()
  const ratio = (e.clientX - rect.left) / Math.max(rect.width, 1)
  const n = props.tSeconds.length
  return Math.min(n - 1, Math.max(0, Math.floor(ratio * n)))
}

function onMove(e: PointerEvent): void {
  const i = indexFromEvent(e)
  if (i === hoverIndex.value) return
  hoverIndex.value = i
  // Без задержки и без запроса: кадр уже в памяти, поэтому глобус успевает за курсором.
  emit('preview', i)
}

function onLeave(): void {
  hoverIndex.value = null
  emit('preview', pinned.value)
}

function onClick(e: PointerEvent): void {
  pinned.value = indexFromEvent(e)
}

const hoverTime = computed(() => {
  const i = hoverIndex.value ?? pinned.value
  const t = props.tSeconds[i] ?? 0
  const hh = String(Math.floor(t / 3600)).padStart(2, '0')
  const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0')
  return `${hh}:${mm}`
})

let observer: ResizeObserver | null = null
onMounted(() => {
  draw()
  if (wrapRef.value) {
    observer = new ResizeObserver(() => draw())
    observer.observe(wrapRef.value)
  }
})
onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})
watch([() => props.clients, () => props.selectedClientId, pinned, hoverIndex], () => draw(), { deep: false })
</script>

<template>
  <div class="flex items-stretch gap-2 sm:gap-3 w-full min-w-0">
    <div class="shrink-0 flex flex-col justify-end pb-1" :style="{ paddingTop: `${TOP}px` }">
      <div
        v-for="c in clients"
        :key="c.clientId"
        class="text-[10px] sm:text-[11px] text-muted leading-none flex items-center justify-end pr-1"
        :style="{ height: `${ROW}px`, marginBottom: `${GAP}px` }"
      >
        <!-- На узком экране длинное имя пункта обрезалось слева и превращалось в обрывок
             («hern terminal 65»). Короткий идентификатор точнее и всегда влезает целиком. -->
        <span class="sm:hidden font-mono">{{ c.clientId }}</span>
        <span class="hidden sm:inline">{{ c.label ?? c.clientId }}</span>
      </div>
    </div>

    <div
      ref="wrapRef"
      class="relative flex-1 min-w-0 cursor-crosshair select-none"
      @pointermove="onMove"
      @pointerleave="onLeave"
      @pointerdown="onClick"
    >
      <canvas ref="canvasRef" class="block w-full" />
      <div
        class="pointer-events-none absolute -top-0.5 translate-x-2 text-[11px] font-mono text-highlighted"
        :style="{ left: `${((hoverIndex ?? pinned) / Math.max(tSeconds.length - 1, 1)) * 100}%` }"
      >
        {{ hoverTime }}
      </div>
    </div>
  </div>
</template>
