<script setup lang="ts">
/**
 * Временная шкала расчёта: скраб по индексу отсчёта сетки `t = 0, step_s, …, horizon_s − step_s`
 * (сама сетка приходит извне пропом `steps` — компонент ничего не знает про `horizon_s`/`step_s`
 * конкретного сценария, только про переданный массив секунд, без хардкода «720 отсчётов»).
 *
 * v-model — ИНДЕКС в `steps`, а не сама секунда: так шкала, диаграмма доступности
 * (`AvailabilityChart.vue`, тот же `currentIndex`) и запрос `snapshot`/выбор кадра из
 * бинарного пакета `compute` (см. `docs/PROTOCOL.md`) держат одну и ту же ось без
 * пересчёта секунд туда-обратно.
 *
 * Собран из готовых компонентов Nuxt UI: `USlider` — сам скраб,
 * `UFieldGroup` + `UButton` — шаг назад/вперёд и play/pause, `USelect` — скорость
 * автопроигрывания, `UBadge` — текущие индекс/t_s/человекочитаемое время, `UKbd` — подсказка
 * по стрелкам рядом с `defineShortcuts`, `UEmpty` — состояние до первого расчёта.
 */

const props = withDefaults(defineProps<{
  /** Сетка отсчётов в секундах — `payload.t_s` пакета `compute`/`attach` (см. `backend/app/protocol.py`). */
  steps: number[]
  /** Начинать ли автопроигрывание сразу после монтирования. */
  autoplay?: boolean
}>(), {
  autoplay: false
})

/** Индекс текущего отсчёта в `steps`, 0-based. */
const index = defineModel<number>({ default: 0 })

const stepCount = computed(() => props.steps.length)
const lastIndex = computed(() => Math.max(stepCount.value - 1, 0))
/** Шаг сетки в секундах — только для форматирования (правая граница последнего отсчёта,
 *  подписи), сама раскладка по индексу его не требует. Резервное значение на случай сетки
 *  из одной точки (шаг посчитать не из чего). */
const stepS = computed(() => (stepCount.value > 1 ? props.steps[1]! - props.steps[0]! : 1))
const currentTs = computed(() => props.steps[index.value] ?? 0)

// Индекс не должен пережить смену сценария на более короткую сетку — иначе слайдер молча
// упирается в несуществующий отсчёт после загрузки другого файла.
watch(stepCount, (n) => {
  if (n === 0) return
  if (index.value > n - 1) index.value = n - 1
  if (index.value < 0) index.value = 0
})

function clampIndex(i: number): number {
  return Math.min(Math.max(i, 0), lastIndex.value)
}
function seekTo(i: number): void {
  index.value = clampIndex(i)
}
function step(delta: number): void {
  seekTo(index.value + delta)
}

// ─────────────────────────────── Автопроигрывание ───────────────────────────────

const playing = ref(false)
const speedOptions = [
  { label: '1×', value: 1 },
  { label: '2×', value: 2 },
  { label: '4×', value: 4 },
  { label: '8×', value: 8 }
]
const speed = ref(speedOptions[0]!.value)
/** Интервал между кадрами на скорости 1× — не физическое время сценария, а комфортный темп
 *  просмотра (720 отсчётов на 1× это 6 минут прогона, на 8× — 45 секунд, хватает на показ). */
const BASE_INTERVAL_MS = 500

let timer: ReturnType<typeof setInterval> | null = null

function stopTimer(): void {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}
function startTimer(): void {
  stopTimer()
  if (stepCount.value <= 1) return
  timer = setInterval(() => {
    // Автопроигрывание зациклено — на защите удобнее не упираться в конец горизонта, а сразу
    // видеть следующий проход; ручной скраб (слайдер/стрелки) по-прежнему не зациклен.
    index.value = index.value >= lastIndex.value ? 0 : index.value + 1
  }, BASE_INTERVAL_MS / speed.value)
}

function play(): void {
  if (stepCount.value <= 1) return
  playing.value = true
}
function pause(): void {
  playing.value = false
}
function togglePlay(): void {
  playing.value ? pause() : play()
}

watch(playing, (v) => (v ? startTimer() : stopTimer()))
watch(speed, () => { if (playing.value) startTimer() })
// Ручной скраб во время проигрывания не должен сбиваться собственным же таймером — просто
// не трогаем playing здесь, USlider меняет index.value напрямую через v-model.
watch(stepCount, (n) => { if (n <= 1) pause() })

onMounted(() => { if (props.autoplay) play() })
onBeforeUnmount(stopTimer)

// ────────────────────────────────── Клавиатура ───────────────────────────────────

defineShortcuts({
  arrowleft: () => step(-1),
  arrowright: () => step(1),
  shift_arrowleft: () => step(-10),
  shift_arrowright: () => step(10),
  home: () => seekTo(0),
  end: () => seekTo(lastIndex.value),
  space: () => togglePlay()
}, { layoutIndependent: true }) // пробел без layoutIndependent триггерится ненадёжно (см. docs/defineShortcuts)

// ────────────────────────────────── Форматирование ───────────────────────────────────

/** Человекочитаемое время от начала расчёта (не UTC — docs/PARAMETERS.md: «точка отсчёта времени
 *  — секунды от начала расчёта»). Горизонт может доходить до 172 800 с (2 суток), поэтому при
 *  переходе через сутки добавляется счётчик дней, а не только часы: minute. */
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

const humanTime = computed(() => formatOffset(currentTs.value))
</script>

<template>
  <UEmpty
    v-if="stepCount === 0"
    icon="i-lucide-clock"
    title="Нет сетки отсчётов"
    description="Запустите расчёт (compute) — шкала появится по сетке t_s из его результата."
  />

  <div v-else class="flex flex-col gap-3">
    <div class="flex flex-wrap items-center gap-2">
      <UFieldGroup>
        <UTooltip text="Предыдущий отсчёт" :kbds="['arrowleft']">
          <UButton
            icon="i-lucide-skip-back"
            color="neutral"
            variant="subtle"
            :disabled="index <= 0"
            aria-label="Предыдущий отсчёт"
            @click="step(-1)"
          />
        </UTooltip>
        <UTooltip :text="playing ? 'Пауза' : 'Проиграть'" :kbds="['Space']">
          <UButton
            :icon="playing ? 'i-lucide-pause' : 'i-lucide-play'"
            color="primary"
            variant="solid"
            :disabled="stepCount <= 1"
            :aria-label="playing ? 'Пауза' : 'Проиграть'"
            @click="togglePlay"
          />
        </UTooltip>
        <UTooltip text="Следующий отсчёт" :kbds="['arrowright']">
          <UButton
            icon="i-lucide-skip-forward"
            color="neutral"
            variant="subtle"
            :disabled="index >= lastIndex"
            aria-label="Следующий отсчёт"
            @click="step(1)"
          />
        </UTooltip>
      </UFieldGroup>

      <USelect
        v-model="speed"
        :items="speedOptions"
        class="w-20"
        size="sm"
        icon="i-lucide-gauge"
        aria-label="Скорость автопроигрывания"
      />

      <div class="flex items-center gap-1.5 ml-auto">
        <UKbd value="arrowleft" size="sm" />
        <UKbd value="arrowright" size="sm" />
        <span class="text-xs text-muted mr-1">по отсчётам</span>
      </div>

      <UBadge color="neutral" variant="subtle" size="md">
        отсчёт {{ index + 1 }} / {{ stepCount }}
      </UBadge>
      <UBadge color="neutral" variant="subtle" size="md" icon="i-lucide-hash">
        t_s = {{ currentTs }}
      </UBadge>
      <UBadge color="primary" variant="subtle" size="md" icon="i-lucide-clock">
        {{ humanTime }} от начала расчёта
      </UBadge>
    </div>

    <USlider
      :model-value="index"
      :min="0"
      :max="lastIndex"
      :step="1"
      tooltip
      aria-label="Отсчёт сетки"
      @update:model-value="(v) => seekTo(Array.isArray(v) ? v[0]! : v)"
    />
  </div>
</template>
