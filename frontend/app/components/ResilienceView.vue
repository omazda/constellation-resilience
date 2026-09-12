<script setup lang="ts">
/**
 * Анализ устойчивости — ответ на вопрос, которого нет в обязательных показателях.
 *
 * Доступность говорит «путь есть». Здесь показано, сколько отказов этот путь переживёт: число
 * вершинно-непересекающихся маршрутов до шлюза (теорема Менгера), поимённо аппараты, чьё изъятие
 * рвёт связь, кратность покрытия наземных линий — и что даёт терпимость к задержке доставки.
 *
 * Выводы под таблицами не зашиты в текст, а собраны из этих же чисел: иначе они врали бы при
 * загрузке другого сценария.
 */
export interface ReserveEntry {
  histogram: Record<string, number>
  reserve_pct: number
  mean: number
}
export interface CoverageEntry {
  histogram: Record<string, number>
  mean: number
}
export interface AnalysisPayload {
  variant_id: string
  reserve: Record<string, ReserveEntry>
  critical_satellites: { satellite_id: string, count: number }[]
  coverage: Record<string, CoverageEntry>
  delivery: { tolerances_s: number[], clients: Record<string, { delivered_pct: Record<string, number> }> }
}

const props = defineProps<{
  analysis: AnalysisPayload | null
  loading?: boolean
  clientLabels?: Record<string, string>
  gatewayIds?: string[]
  targetAvailability?: number
}>()

const emit = defineEmits<{ request: [] }>()

const pct = (x: number) => `${(x * 100).toFixed(2)} %`
const label = (id: string) => props.clientLabels?.[id] ?? id
const minutes = (s: number) => (s === 0 ? 'сразу' : s < 3600 ? `${Math.round(s / 60)} мин` : `${Math.round(s / 3600)} ч`)

/** Самое узкое место по кратности покрытия — то, чем ограничен резерв. */
const bottleneck = computed(() => {
  const entries = Object.entries(props.analysis?.coverage ?? {})
  if (!entries.length) return null
  const [id, e] = entries.reduce((a, b) => (a[1].mean <= b[1].mean ? a : b))
  return { id, mean: e.mean, single: e.histogram['1'] ?? 0, none: e.histogram['0'] ?? 0 }
})

/** Допуск по задержке, при котором цель выполняется по всем пунктам, если такой есть. */
const targetByDelay = computed(() => {
  const d = props.analysis?.delivery
  if (!d) return null
  const target = props.targetAvailability ?? 0.9
  for (const t of d.tolerances_s) {
    const all = Object.values(d.clients).every(c => (c.delivered_pct[String(t)] ?? 0) >= target)
    if (all) return t
  }
  return null
})

const worstReserve = computed(() => {
  const entries = Object.entries(props.analysis?.reserve ?? {})
  if (!entries.length) return null
  return entries.reduce((a, b) => (a[1].reserve_pct <= b[1].reserve_pct ? a : b))
})
</script>

<template>
  <div v-if="!analysis" class="h-full flex items-center justify-center p-6">
    <UEmpty
      icon="i-lucide-shield-alert"
      title="Анализ устойчивости"
      description="Резерв маршрутов, критические аппараты и доставка с допуском по задержке. Считается отдельным проходом по горизонту — несколько секунд."
    >
      <template #actions>
        <UButton label="Посчитать" icon="i-lucide-play" :loading="loading" @click="emit('request')" />
      </template>
    </UEmpty>
  </div>

  <div v-else class="p-4 space-y-6">
    <section class="space-y-2">
      <h3 class="text-sm font-medium text-highlighted">Резерв маршрутов</h3>
      <p class="text-xs text-muted">
        Сколько независимых маршрутов до шлюза существует одновременно. Один маршрут означает, что
        отказ единственного аппарата рвёт связь.
      </p>
      <div class="space-y-1.5">
        <div v-for="(r, id) in analysis.reserve" :key="id" class="flex items-center gap-3 text-sm">
          <span class="w-44 shrink-0 truncate">{{ label(id) }}</span>
          <UProgress :model-value="r.reserve_pct * 100" size="sm" class="flex-1" />
          <span class="font-mono tabular text-xs w-20 text-right">{{ pct(r.reserve_pct) }}</span>
          <UBadge :label="`в среднем ${r.mean.toFixed(2)}`" color="neutral" variant="subtle" size="sm" />
        </div>
      </div>
      <p v-if="worstReserve" class="text-xs text-toned">
        Хуже всего у пункта <b>{{ label(worstReserve[0]) }}</b>: резерв есть лишь
        {{ pct(worstReserve[1].reserve_pct) }} времени — остальное время связь держится на одном маршруте.
      </p>
    </section>

    <section class="space-y-2">
      <h3 class="text-sm font-medium text-highlighted">Критические аппараты</h3>
      <p class="text-xs text-muted">Изъятие каждого рвёт связь хотя бы одному пункту. Число — в скольких отсчётах.</p>
      <div class="flex flex-wrap gap-1.5">
        <UBadge
          v-for="c in analysis.critical_satellites"
          :key="c.satellite_id"
          :label="`${c.satellite_id} · ${c.count}`"
          color="error"
          variant="subtle"
        />
        <span v-if="!analysis.critical_satellites.length" class="text-xs text-muted">
          Аппаратов, чьё изъятие рвёт связь, не найдено — сеть всюду имеет обход.
        </span>
      </div>
    </section>

    <section class="space-y-2">
      <h3 class="text-sm font-medium text-highlighted">Кратность покрытия</h3>
      <p class="text-xs text-muted">Сколько аппаратов узел видит одновременно — этим и ограничен резерв.</p>
      <div class="space-y-1">
        <div v-for="(c, id) in analysis.coverage" :key="id" class="flex items-center gap-3 text-sm">
          <span class="w-44 shrink-0 truncate">
            {{ label(id) }}
            <UBadge v-if="gatewayIds?.includes(id)" label="шлюз" color="secondary" variant="subtle" size="sm" />
          </span>
          <span class="font-mono tabular text-xs">в среднем {{ c.mean.toFixed(2) }}</span>
          <span class="text-xs text-muted">
            один аппарат — {{ pct(c.histogram['1'] ?? 0) }}, ни одного — {{ pct(c.histogram['0'] ?? 0) }}
          </span>
        </div>
      </div>
      <p v-if="bottleneck" class="text-xs text-toned">
        Узкое место — <b>{{ label(bottleneck.id) }}</b>: {{ pct(bottleneck.single) }} времени видит ровно
        один аппарат, значит любой второй маршрут обязан пройти через него же.
      </p>
    </section>

    <section class="space-y-2">
      <h3 class="text-sm font-medium text-highlighted">Доставка с допуском по задержке</h3>
      <p class="text-xs text-muted">
        Доля отсчётов, из которых данные дойдут до шлюза, если разрешить подождать на борту
        и уйти следующим контактом.
      </p>
      <div class="overflow-x-auto">
        <table class="text-sm w-full">
          <thead>
            <tr class="text-xs text-muted">
              <th class="text-left font-normal py-1 pr-3">Пункт</th>
              <th v-for="t in analysis.delivery.tolerances_s" :key="t" class="text-right font-normal py-1 px-2">
                {{ minutes(t) }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(c, id) in analysis.delivery.clients" :key="id" class="border-t border-default">
              <td class="py-1 pr-3 truncate max-w-44">{{ label(id) }}</td>
              <td
                v-for="t in analysis.delivery.tolerances_s"
                :key="t"
                class="text-right font-mono tabular text-xs py-1 px-2"
                :class="(c.delivered_pct[String(t)] ?? 0) >= (targetAvailability ?? 0.9) ? 'text-success' : ''"
              >
                {{ pct(c.delivered_pct[String(t)] ?? 0) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="text-xs text-toned">
        <template v-if="targetByDelay !== null">
          Цель выполняется по всем пунктам, если допустить задержку доставки
          <b>{{ minutes(targetByDelay) }}</b>. Там, где мгновенная доступность ниже цели, проблема
          в разрыве сети, а не в покрытии.
        </template>
        <template v-else>
          Задержка доставки цели не спасает: пункт часто не видит ни одного аппарата — проблема
          в покрытии, и лечится она числом аппаратов, а не маршрутизацией.
        </template>
      </p>
    </section>
  </div>
</template>
