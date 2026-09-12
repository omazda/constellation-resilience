<script setup lang="ts">
/**
 * Рабочий экран: глобус группировки слева, шкала/диаграммы/сравнение справа — разделены
 * `USplitter`.
 *
 * Что откуда берётся (docs/PROTOCOL.md):
 *  • `compute` — весь горизонт одним пакетом: сетка отсчётов, ряды доступности, сводные
 *    показатели. Перемотка шкалы после этого идёт без единого запроса.
 *  • глобус рисуется из того же пакета через `frameAt` — запросов при перемотке нет вообще.
 *    Запрос `snapshot` остаётся в контракте для точечных проверок и отладки, но интерфейсу
 *    он больше не нужен: сутки целиком уже лежат в памяти браузера.
 */
import type { SplitterItem, TabsItem } from '@nuxt/ui'
import type { GlobeGroundContact, GlobeGroundSite, GlobeIslEdge, GlobeRoute, GlobeSatellite } from '../components/GlobeView.vue'
import type { CompareVariantOption } from '../components/CompareView.vue'
import type { AnalysisPayload } from '../components/ResilienceView.vue'
import { useMediaQuery } from '@vueuse/core'
import { decodeResult, type DecodedResult, type ResultManifest } from '../composables/useResult'

const { request } = useWs()
const { variantId: loadedVariantId, summary } = useScenario()
const toast = useToast()

/** Вариант, который сейчас считается: исходный из `scenario.load` либо тот, что вернул
 *  `ConfigPanel` после правки. */
const activeVariantId = ref<string | null>(null)
watch(loadedVariantId, (id) => { if (id) activeVariantId.value = id }, { immediate: true })

// ─────────────────────────────── пакет расчёта ───────────────────────────────
const result = ref<DecodedResult | null>(null)
const computing = ref(false)
const progressPct = ref(0)
const currentIndex = ref(0)

/**
 * `currentIndex` — закреплённый отсчёт (клик по полосе, шкала, клавиатура).
 * `previewIndex` — то, что показывает глобус прямо сейчас: при наведении на полосу суток он
 * бежит за курсором, при уходе возвращается к закреплённому. Разделение нужно, чтобы беглый
 * просмотр не сбивал выбранный момент.
 */
const previewIndex = ref(0)
watch(currentIndex, (i) => { previewIndex.value = i })

/**
 * Стратегия поиска маршрута. Доступность и максимальный перерыв от неё НЕ зависят — это
 * достижимость в графе (docs/PARAMETERS.md §5), и переключатель это показывает наглядно:
 * проценты не меняются, а длина маршрута и число переходов меняются. Ровно то, что просит
 * критерий «Алгоритмы маршрутизации»: объяснить алгоритм и показать его работу.
 */
const strategy = ref<'hops' | 'distance'>('hops')
const strategyItems = [
  { label: 'по числу переходов', value: 'hops' },
  { label: 'по длине трассы', value: 'distance' }
]

const effectiveScenario = computed(() => (summary.value?.effective_scenario ?? null) as {
  environment?: { altitude_km?: number, target_availability?: number }
  ground_sites?: GlobeGroundSite[]
} | null)

const groundSites = computed<GlobeGroundSite[]>(() => effectiveScenario.value?.ground_sites ?? [])
const clientLabels = computed<Record<string, string>>(() =>
  Object.fromEntries(groundSites.value.filter(g => g.role === 'client').map(g => [g.id, g.name])))
const targetAvailability = computed(() => effectiveScenario.value?.environment?.target_availability ?? 0.9)
const altitudeKm = computed(() => effectiveScenario.value?.environment?.altitude_km ?? null)

const selectedClientId = ref<string | null>(null)
watch(result, (r) => {
  if (r && (!selectedClientId.value || !r.clients.includes(selectedClientId.value))) {
    selectedClientId.value = r.clients[0] ?? null
  }
})

async function loadResult(variantId: string): Promise<void> {
  computing.value = true
  progressPct.value = 0
  try {
    const manifest = await request<ResultManifest>('compute', { variant_id: variantId, strategy: strategy.value }, {
      onProgress: (p) => { progressPct.value = Number((p as { pct?: number })?.pct ?? 0) }
    })
    result.value = decodeResult(manifest, clientLabels.value, altitudeKm.value ?? 550)
    currentIndex.value = 0
    previewIndex.value = 0
  } catch {
    // Тело ошибки уже показано тостом в useWs; здесь только снимаем состояние расчёта.
    result.value = null
  } finally {
    computing.value = false
  }
}

watch(activeVariantId, (id) => { if (id) void loadResult(id) })
// Смена стратегии — пересчёт того же варианта другим алгоритмом.
watch(strategy, () => { if (activeVariantId.value) void loadResult(activeVariantId.value) })

// Кадр сети собирается из уже привезённого пакета — ни одного запроса при перемотке.
// Это и есть причина, по которой картинка успевает за курсором.
const frame = computed(() => result.value?.frameAt(previewIndex.value) ?? null)
const trails = computed(() => result.value?.trailAt(previewIndex.value, 8) ?? [])

// ─────────────────────────────── сравнение вариантов ───────────────────────────────
/**
 * Варианты берутся СПИСКОМ С СЕРВЕРА, а не копятся во вкладке браузера: они лежат на диске и
 * переживают перезапуск сервиса, поэтому сравнение предлагает выбор из всех уже загруженных
 * конфигураций, включая те, что делали в прошлый раз.
 */
interface StoredVariant {
  variant_id: string
  title?: string
  source?: string
  created?: string
  summary?: { launch_stage?: number, n_satellites?: number }
}

const storedVariants = ref<StoredVariant[]>([])
const variantIdA = ref<string | null>(null)
const variantIdB = ref<string | null>(null)
const compareResult = ref<unknown>(null)
const compareLoading = ref(false)

const savedVariants = computed<CompareVariantOption[]>(() => storedVariants.value.map(v => ({
  variantId: v.variant_id,
  label: `${v.title ?? 'Вариант'} · очередь ${v.summary?.launch_stage ?? '?'} · ${v.variant_id.slice(0, 8)}`
})))

async function refreshVariants(): Promise<void> {
  try {
    const r = await request<{ variants: StoredVariant[] }>('variants.list')
    storedVariants.value = r.variants ?? []
    // Пара для сравнения подставляется сама: два последних варианта — самый частый случай.
    const ids = storedVariants.value.map(v => v.variant_id)
    if (!variantIdA.value || !ids.includes(variantIdA.value)) variantIdA.value = ids.at(-2) ?? ids.at(-1) ?? null
    if (!variantIdB.value || !ids.includes(variantIdB.value)) variantIdB.value = ids.at(-1) ?? null
  } catch { /* тост уже показан */ }
}

onMounted(() => { void refreshVariants() })
watch(loadedVariantId, () => { void refreshVariants() })

function onVariantChanged(payload: { variantId: string, summary: Record<string, unknown> }): void {
  activeVariantId.value = payload.variantId
  void refreshVariants()
}

/** Переключиться на сохранённый вариант — возврат к нему требует ТЗ (п. 1 функциональных). */
function openVariant(variantId: string): void {
  activeVariantId.value = variantId
}

async function runCompare(): Promise<void> {
  if (!variantIdA.value || !variantIdB.value) return
  compareLoading.value = true
  try {
    compareResult.value = await request('compare', {
      variant_id_a: variantIdA.value,
      variant_id_b: variantIdB.value,
      strategy: strategy.value
    })
  } catch {
    compareResult.value = null
  } finally {
    compareLoading.value = false
  }
}

// ─────────────────────────────── анализ устойчивости ───────────────────────────────
// Считается отдельным запросом по кнопке: проход по горизонту занимает несколько секунд,
// а нужен не на каждой перемотке. Сервер кеширует результат по варианту.
const analysis = ref<AnalysisPayload | null>(null)
const analysisLoading = ref(false)

async function runAnalysis(): Promise<void> {
  const variantId = activeVariantId.value
  if (!variantId) return
  analysisLoading.value = true
  try {
    analysis.value = await request<AnalysisPayload>('analysis', { variant_id: variantId })
  } catch {
    analysis.value = null
  } finally {
    analysisLoading.value = false
  }
}

// Смена варианта обесценивает прошлый анализ — он считался для другой конфигурации.
watch(activeVariantId, () => { analysis.value = null })

const gatewayIds = computed(() => groundSites.value.filter(g => g.role === 'gateway').map(g => g.id))

// ─────────────────────────────── раскладка ───────────────────────────────
// Пять вкладок в узкой колонке: с иконками подписи обрезаются до «Усто…» и перестают
// что-либо значить. Иконка здесь несёт меньше смысла, чем слово, поэтому уходит она.
const tabs = computed<TabsItem[]>(() => [
  { label: 'Маршруты', slot: 'timeline' },
  { label: 'Доступность', slot: 'availability' },
  { label: 'Устойчивость', slot: 'resilience' },
  { label: 'Настройка', slot: 'config' },
  { label: 'Сравнение', slot: 'compare' }
])

/**
 * На узком экране горизонтальный сплиттер не имеет смысла: глобусу и панели остаётся по
 * трети экрана, и не читается ни то, ни другое. Ниже 1024 px раскладка складывается в столбец —
 * глобус сверху фиксированной высотой, полоса суток под ним, вкладки прокручиваются следом.
 */
const isWide = useMediaQuery('(min-width: 1024px)')

const splitterItems = computed<SplitterItem[]>(() => (
  isWide.value
    ? [
        { id: 'panel-globe', slot: 'globe', minSize: 30, defaultSize: 54 },
        { id: 'panel-charts', slot: 'panels', minSize: 30, defaultSize: 46 }
      ]
    // В столбце глобусу нужна бо́льшая доля: сжатый по высоте шар не читается вовсе,
    // а вкладки прокручиваются.
    : [
        { id: 'panel-globe', slot: 'globe', minSize: 35, defaultSize: 62 },
        { id: 'panel-charts', slot: 'panels', minSize: 25, defaultSize: 38 }
      ]
))

const currentRoutes = computed<GlobeRoute[]>(() => (frame.value?.routes ?? []) as GlobeRoute[])

function exportDocument(kind: 'result' | 'scenario'): void {
  const variantId = activeVariantId.value
  if (!variantId) return
  void request('export', { variant_id: variantId, kind }).then((doc) => {
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = kind === 'result' ? `result-${variantId.slice(0, 8)}.json` : `scenario-${variantId.slice(0, 8)}.json`
    a.click()
    URL.revokeObjectURL(url)
    toast.add({ title: 'Выгружено', description: a.download, color: 'success' })
  })
}
</script>

<template>
  <!--
    Панель инструментов — обычная строка в теле страницы, а не слот #header у UDashboardPanel:
    в этой сборке слот не рендерится вовсе (проверено в браузере — элемента панели нет в DOM),
    и вместе с ним молча пропадали выгрузка и переключатель стратегии.
  -->
  <div class="h-full w-full min-w-0 flex flex-col overflow-hidden">
    <div class="shrink-0 flex flex-wrap items-center gap-2 px-3 py-2 border-b border-default bg-default">
      <UDashboardSidebarToggle class="lg:hidden" />
      <UDashboardSidebarCollapse class="hidden lg:inline-flex" />

      <UBadge
        v-if="computing"
        :label="`Расчёт ${progressPct} %`"
        color="info"
        variant="subtle"
        icon="i-lucide-loader-circle"
      />
      <USelectMenu
        v-else-if="savedVariants.length"
        :model-value="activeVariantId ?? undefined"
        :items="savedVariants"
        value-key="variantId"
        label-key="label"
        icon="i-lucide-git-branch"
        size="xs"
        class="w-56 sm:w-72"
        placeholder="Сохранённые варианты"
        @update:model-value="openVariant($event as string)"
      />
      <UBadge v-else label="Сценарий не загружен" color="neutral" variant="subtle" icon="i-lucide-upload" />

      <div class="flex-1 min-w-0" />

      <template v-if="result">
        <span class="text-xs text-muted hidden sm:inline">Маршрут ищем</span>
        <USelectMenu
          v-model="strategy"
          :items="strategyItems"
          value-key="value"
          size="xs"
          class="w-44"
          :disabled="computing"
        />
        <UTooltip text="Выгрузить результат расчёта">
          <UButton
            icon="i-lucide-download"
            color="neutral"
            variant="subtle"
            size="xs"
            aria-label="Выгрузить результат расчёта"
            @click="exportDocument('result')"
          >
            <span class="hidden sm:inline">Результат</span>
          </UButton>
        </UTooltip>
        <UTooltip text="Выгрузить изменённый сценарий">
          <UButton
            icon="i-lucide-file-json"
            color="neutral"
            variant="subtle"
            size="xs"
            aria-label="Выгрузить изменённый сценарий"
            @click="exportDocument('scenario')"
          >
            <span class="hidden sm:inline">Сценарий</span>
          </UButton>
        </UTooltip>
      </template>
    </div>

    <div class="flex-1 min-h-0 flex flex-col">
      <USplitter
        id="workspace-splitter"
        :items="splitterItems"
        :orientation="isWide ? 'horizontal' : 'vertical'"
        class="flex-1 min-h-0 w-full min-w-0"
      >
        <template #globe>
          <div class="h-full w-full min-w-0 flex flex-col">
            <div class="flex-1 min-h-0 relative">
            <GlobeView
              v-if="frame"
              :satellites="frame.satellites"
              :isl-edges="frame.islEdges"
              :ground-contacts="frame.groundContacts"
              :ground-sites="groundSites"
              :routes="currentRoutes"
              :trails="trails"
              :selected-client-id="selectedClientId"
              :altitude-km="altitudeKm"
              @select-client="selectedClientId = $event"
            />
            <div v-else class="h-full flex items-center justify-center p-6">
              <UEmpty
                icon="i-lucide-globe"
                :title="computing ? 'Идёт расчёт' : 'Глобус группировки'"
                :description="computing
                  ? `Считается ${progressPct}% горизонта — весь пакет приходит одним ответом.`
                  : 'Загрузите сценарий в панели слева: появятся аппараты, связи и маршрут до шлюза.'"
              />
            </div>
            </div>

            <div v-if="result" class="shrink-0 border-t border-default px-3 py-2.5 bg-default">
              <DayBand
                v-model="currentIndex"
                :t-seconds="result.steps"
                :clients="result.series"
                :period-s="result.islPeriodS"
                :selected-client-id="selectedClientId"
                @preview="previewIndex = $event"
              />
            </div>
          </div>
        </template>

        <template #panels>
          <UTabs
            :items="tabs"
            class="h-full w-full min-w-0"
            :ui="{
              root: 'min-w-0',
              list: 'min-w-0 overflow-x-auto',
              trigger: 'shrink-0',
              content: 'h-full min-w-0 overflow-auto'
            }"
          >
            <template #timeline>
              <div class="h-full min-w-0 p-4 space-y-4">
                <template v-if="result">
                  <TimelineBar v-model="currentIndex" :steps="result.steps" />

                  <div class="space-y-2">
                    <div
                      v-for="route in currentRoutes"
                      :key="route.client_id"
                      class="flex items-center gap-2 text-sm cursor-pointer rounded px-2 py-1.5"
                      :class="route.client_id === selectedClientId ? 'bg-elevated' : ''"
                      @click="selectedClientId = route.client_id"
                    >
                      <UBadge
                        :label="clientLabels[route.client_id] ?? route.client_id"
                        :color="route.path.length ? 'primary' : 'error'"
                        variant="subtle"
                      />
                      <span v-if="route.path.length" class="text-muted truncate">
                        {{ route.path.join(' → ') }}
                      </span>
                      <span v-else class="text-error">нет маршрута: {{ route.reason }}</span>
                      <UBadge
                        v-if="route.hops !== null"
                        :label="`${route.hops} переходов`"
                        color="neutral"
                        variant="subtle"
                        size="sm"
                        class="ml-auto shrink-0"
                      />
                    </div>
                  </div>
                </template>

                <div v-else class="h-full flex items-center justify-center">
                  <UEmpty icon="i-lucide-clock" title="Временная шкала" description="Появится после расчёта сценария." />
                </div>
              </div>
            </template>

            <template #availability>
              <div class="h-full min-w-0 p-4">
                <AvailabilityChart
                  v-if="result"
                  :t-seconds="result.steps"
                  :clients="result.series"
                  :current-index="currentIndex"
                  :target-availability="targetAvailability"
                  @seek="currentIndex = $event"
                />
                <div v-else class="h-full flex items-center justify-center">
                  <UEmpty
                    icon="i-lucide-activity"
                    title="Доступность и перерывы"
                    description="Интервалы связи и перерывы по каждому пункту — после расчёта."
                  />
                </div>
              </div>
            </template>

            <template #resilience>
              <ResilienceView
                :analysis="analysis"
                :loading="analysisLoading"
                :client-labels="clientLabels"
                :gateway-ids="gatewayIds"
                :target-availability="targetAvailability"
                @request="runAnalysis"
              />
            </template>

            <template #config>
              <div class="h-full min-w-0 p-4">
                <ConfigPanel @variant-changed="onVariantChanged" />
              </div>
            </template>

            <template #compare>
              <div class="h-full min-w-0 p-4">
                <CompareView
                  v-model:variant-id-a="variantIdA"
                  v-model:variant-id-b="variantIdB"
                  :variants="savedVariants"
                  :result="compareResult as any"
                  :loading="compareLoading"
                  :target-availability="targetAvailability"
                  :client-labels="clientLabels"
                  @recompare="runCompare"
                />
              </div>
            </template>
          </UTabs>
        </template>
      </USplitter>
    </div>
  </div>
</template>
