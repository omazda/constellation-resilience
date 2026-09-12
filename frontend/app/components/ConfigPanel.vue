<script setup lang="ts">
/**
 * Редактирование конфигурации текущего сценария: очередь запуска, RAAN/фаза плоскостей, периоды
 * недоступности аппаратов — DoD п.1. Правка не считается локально: она уходит на сервер
 * сообщением `scenario.patch` (`.claude/rules/protocol.md`, `core.scenario.patch_scenario`) —
 * сервер единственный источник истины по валидации границ, здесь только сбор дельты формы
 * и удобный UI. Ответ несёт НОВЫЙ `variant_id`: сценарии в сервисе неизменяемы, «сохранить
 * вариант» и есть «получить новый variant_id и запомнить его».
 *
 * Допущение (в ТЗ кнопка не описана буквально, решение принято самостоятельно): «Новый запуск»
 * возвращает форму к исходно загруженному сценарию (variant_id из `scenario.load`), отменяя все
 * патчи текущей сессии, но НЕ трогает уже сохранённые варианты — они остаются в списке снизу,
 * и к ним по-прежнему можно вернуться. Это отдельное действие от «Сбросить изменения», которое
 * отменяет только черновые правки, ещё не сохранённые как вариант.
 *
 * Красный флаг, которого здесь нет: ни одного зашитого ID плоскости/аппарата/пункта — состав
 * плоскостей, список аппаратов для выбора при добавлении отказа и счётчики по очередям запуска
 * целиком читаются из загруженного сценария (см. `docs/PARAMETERS.md`, раздел «Красные флаги»
 * в CLAUDE.md).
 */
import type { FormError, FormSubmitEvent, TableColumn } from '@nuxt/ui'

// ── форма данных сценария (подмножество cosmo-A-1.0, которым управляет эта панель) ────────────

interface PlaneDef { id: string; raan_deg: number; phase_deg: number }
interface SatelliteDef { id: string; plane_id: string; slot_deg: number; launch_batch: 1 | 2 | 3 }
interface FailureEntry { satellite_id: string; start_s: number; end_s: number }

interface EffectiveScenario {
  schema_version: string
  meta?: { id?: string; title?: string; [key: string]: unknown } | null
  environment: { horizon_s: number; step_s: number; [key: string]: unknown }
  design: { launch_stage: 1 | 2 | 3; planes: PlaneDef[]; satellites: SatelliteDef[] }
  ground_sites: unknown[]
  failures: FailureEntry[]
  gateway_outages: unknown[]
}

/** Форма ответа `scenario.load`/`scenario.patch` — контракт зафиксирован дословно
 *  в `.claude/rules/protocol.md`; `useScenario().summary` типизирован шире (см. её докстринг),
 *  поэтому здесь свой узкий тип и проверка формы перед использованием (`asEffectiveScenario`). */
interface ScenarioCommitPayload {
  variant_id: string
  scenario_hash: string
  parent_variant_id?: string
  effective_scenario: EffectiveScenario
  summary: Record<string, unknown>
}

interface DraftState {
  launchStage: 1 | 2 | 3
  planes: Record<string, { raan_deg: number; phase_deg: number }>
}

interface ScenarioPatch {
  launch_stage?: 1 | 2 | 3
  planes?: Record<string, { raan_deg?: number; phase_deg?: number }>
  add_failures?: FailureEntry[]
  remove_failures?: FailureEntry[]
}

interface SavedVariant {
  variantId: string
  label: string
  summary: Record<string, unknown>
  effectiveScenario: EffectiveScenario
  savedAt: Date
}

interface FailureRow {
  satelliteId: string
  startS: number
  endS: number
  durationS: number
  status: 'existing' | 'pending-add' | 'pending-remove'
  source: FailureEntry
}

const emit = defineEmits<{
  /** Текущий (актуальный для расчёта) вариант сменился — сохранение, возврат к сохранённому
   *  или «новый запуск». Интеграция: карта/шкала/сравнение должны запрашивать `compute`/
   *  `snapshot` для этого `variantId`, а не для того, что вернул исходный `scenario.load`. */
  'variant-changed': [payload: { variantId: string; summary: Record<string, unknown> }]
}>()

const toast = useToast()
const { request } = useWs()
const { summary: scenarioSummary, variantId: loadedVariantId } = useScenario()

function asEffectiveScenario(value: unknown): EffectiveScenario | null {
  if (!value || typeof value !== 'object') return null
  const v = value as Record<string, unknown>
  if (typeof v.environment !== 'object' || v.environment === null) return null
  if (typeof v.design !== 'object' || v.design === null) return null
  return value as EffectiveScenario
}

// ── состояние: исходно загруженный сценарий / текущий сохранённый / черновик формы ────────────

const originalVariantId = ref<string | null>(null)
const originalScenario = ref<EffectiveScenario | null>(null)
const originalSummary = ref<Record<string, unknown> | undefined>(undefined)

/** Вариант, который сервер запомнил последним (последнее `scenario.patch`/возврат к сохранённому,
 *  или исходная загрузка, если правок ещё не было) — база, относительно которой строится дельта
 *  формы (`buildPatch`) и на которую откатывает «Сбросить изменения». */
const committedVariantId = ref<string | null>(null)
const committedScenario = ref<EffectiveScenario | null>(null)
const committedSummary = ref<Record<string, unknown> | undefined>(undefined)

const draft = reactive<DraftState>({ launchStage: 1, planes: {} })
const pendingAdd = ref<FailureEntry[]>([])
const pendingRemove = ref<FailureEntry[]>([])
const saveLabel = ref('')
const newFailure = reactive<{ satelliteId: string | undefined; startS: number; endS: number }>({
  satelliteId: undefined,
  startS: 0,
  endS: 3600
})

const savedVariants = ref<SavedVariant[]>([])
const isOutagesOpen = ref(false)
const saving = ref(false)
const exporting = ref(false)
const busy = computed(() => saving.value || exporting.value)

/** Возвращает черновик формы к текущему сохранённому варианту — используется и при первой
 *  загрузке, и после сохранения/возврата (черновик снова совпадает с базой), и по кнопке
 *  «Сбросить изменения». */
function resetDraft(): void {
  const es = committedScenario.value
  if (!es) return
  draft.launchStage = es.design.launch_stage
  draft.planes = Object.fromEntries(
    es.design.planes.map(p => [p.id, { raan_deg: p.raan_deg, phase_deg: p.phase_deg }])
  )
  pendingAdd.value = []
  pendingRemove.value = []
  saveLabel.value = ''
  newFailure.satelliteId = undefined
  newFailure.startS = 0
  newFailure.endS = Math.min(3600, es.environment.horizon_s)
}

function adoptCommitted(variantId: string, es: EffectiveScenario, summary?: Record<string, unknown>): void {
  committedVariantId.value = variantId
  committedScenario.value = es
  if (summary) committedSummary.value = summary
  resetDraft()
}

watch(loadedVariantId, (id) => {
  if (!id) {
    originalVariantId.value = null
    originalScenario.value = null
    originalSummary.value = undefined
    committedVariantId.value = null
    committedScenario.value = null
    committedSummary.value = undefined
    savedVariants.value = []
    return
  }
  const es = asEffectiveScenario(scenarioSummary.value?.effective_scenario)
  if (!es) return
  originalVariantId.value = id
  originalScenario.value = es
  originalSummary.value = scenarioSummary.value?.summary as Record<string, unknown> | undefined
  // Новая загрузка сценария (не патч этой панели) — прежняя история сохранённых вариантов
  // относилась к другому файлу и дальше не имеет смысла.
  savedVariants.value = []
  adoptCommitted(id, es, originalSummary.value)
}, { immediate: true })

// ── сводки для формы — только из самого сценария, никаких зашитых количеств/ID ────────────────

const planeList = computed<PlaneDef[]>(() => committedScenario.value?.design.planes ?? [])
const totalSatellites = computed(() => committedScenario.value?.design.satellites.length ?? 0)
const horizonS = computed(() => committedScenario.value?.environment.horizon_s ?? 0)

const batchCounts = computed<Record<number, number>>(() => {
  const counts: Record<number, number> = {}
  for (const sat of committedScenario.value?.design.satellites ?? []) {
    counts[sat.launch_batch] = (counts[sat.launch_batch] ?? 0) + 1
  }
  return counts
})

function cumulativeActive(upTo: number): number {
  let total = 0
  for (let batch = 1; batch <= upTo; batch += 1) total += batchCounts.value[batch] ?? 0
  return total
}

const launchStageItems = computed(() => ([1, 2, 3] as const).map(stage => ({
  label: `Очередь ${stage}`,
  description: `активно ${cumulativeActive(stage)} из ${totalSatellites.value} аппаратов`,
  value: stage
})))

const satelliteOptions = computed(() => (committedScenario.value?.design.satellites ?? []).map(sat => ({
  label: `${sat.id} · ${sat.plane_id} · очередь ${sat.launch_batch}`,
  value: sat.id
})))

// ── дельта формы относительно текущего сохранённого варианта ──────────────────────────────────

function approxEqual(a: number, b: number): boolean {
  return Math.abs(a - b) < 1e-9
}

/** Собирает патч в формате `core.scenario.patch_scenario` из того, что реально изменилось
 *  в черновике, — пустые поля на сервер не уходят (иначе безобидные «те же RAAN ещё раз»
 *  раздували бы историю вариантов одинаковыми записями). */
function buildPatch(): ScenarioPatch {
  const es = committedScenario.value
  const patch: ScenarioPatch = {}
  if (!es) return patch
  if (draft.launchStage !== es.design.launch_stage) patch.launch_stage = draft.launchStage

  const planesPatch: Record<string, { raan_deg?: number; phase_deg?: number }> = {}
  for (const plane of es.design.planes) {
    const d = draft.planes[plane.id]
    if (!d) continue
    const diff: { raan_deg?: number; phase_deg?: number } = {}
    if (!approxEqual(d.raan_deg, plane.raan_deg)) diff.raan_deg = d.raan_deg
    if (!approxEqual(d.phase_deg, plane.phase_deg)) diff.phase_deg = d.phase_deg
    if (Object.keys(diff).length > 0) planesPatch[plane.id] = diff
  }
  if (Object.keys(planesPatch).length > 0) patch.planes = planesPatch

  if (pendingAdd.value.length > 0) patch.add_failures = pendingAdd.value.slice()
  if (pendingRemove.value.length > 0) patch.remove_failures = pendingRemove.value.slice()
  return patch
}

const isDirty = computed(() => Object.keys(buildPatch()).length > 0)
const canStartOver = computed(() =>
  originalVariantId.value !== null && (committedVariantId.value !== originalVariantId.value || isDirty.value)
)

function describeChanges(patch: ScenarioPatch): string {
  const parts: string[] = []
  if (patch.launch_stage !== undefined) parts.push(`очередь ${patch.launch_stage}`)
  if (patch.planes) {
    for (const [planeId, values] of Object.entries(patch.planes)) {
      const bits: string[] = []
      if (values.raan_deg !== undefined) bits.push(`RAAN ${values.raan_deg}°`)
      if (values.phase_deg !== undefined) bits.push(`фаза ${values.phase_deg}°`)
      if (bits.length > 0) parts.push(`${planeId}: ${bits.join(', ')}`)
    }
  }
  if (patch.add_failures?.length) parts.push(`+${patch.add_failures.length} период(ов) недоступности`)
  if (patch.remove_failures?.length) parts.push(`−${patch.remove_failures.length} период(ов) недоступности`)
  return parts.length > 0 ? parts.join(' · ') : 'без изменений'
}

function describeSummary(summary: Record<string, unknown>): string {
  return `очередь ${summary.launch_stage} · отказов: ${summary.n_failures}`
}

// ── валидация форм: те же границы, что проверит сервер, — чтобы не гонять заведомо невалидный
// патч по сети и сразу показать причину рядом с полем (а не общим тостом) ─────────────────────

function validateDraft(state: DraftState): FormError[] {
  const errors: FormError[] = []
  for (const [planeId, plane] of Object.entries(state.planes)) {
    if (!(plane.raan_deg >= 0 && plane.raan_deg < 360)) {
      errors.push({ name: `planes.${planeId}.raan_deg`, message: 'RAAN должен быть в диапазоне [0, 360)' })
    }
    if (!(plane.phase_deg >= 0 && plane.phase_deg < 360)) {
      errors.push({ name: `planes.${planeId}.phase_deg`, message: 'Фаза должна быть в диапазоне [0, 360)' })
    }
  }
  return errors
}

function failureKeyOf(f: FailureEntry): string {
  return `${f.satellite_id}|${f.start_s}|${f.end_s}`
}

function isDuplicateFailure(entry: FailureEntry): boolean {
  const key = failureKeyOf(entry)
  const removedKeys = new Set(pendingRemove.value.map(failureKeyOf))
  const inCommitted = (committedScenario.value?.failures ?? [])
    .some(f => failureKeyOf(f) === key && !removedKeys.has(key))
  const inPendingAdd = pendingAdd.value.some(f => failureKeyOf(f) === key)
  return inCommitted || inPendingAdd
}

function validateNewFailure(state: { satelliteId: string | undefined; startS: number; endS: number }): FormError[] {
  const errors: FormError[] = []
  if (!state.satelliteId) errors.push({ name: 'satelliteId', message: 'Выберите аппарат' })
  if (!Number.isFinite(state.startS) || state.startS < 0) {
    errors.push({ name: 'startS', message: 'Должно быть ≥ 0' })
  }
  if (!Number.isFinite(state.endS) || state.endS > horizonS.value) {
    errors.push({ name: 'endS', message: `Должно быть ≤ ${horizonS.value} с (горизонт сценария)` })
  }
  if (Number.isFinite(state.startS) && Number.isFinite(state.endS) && state.startS >= state.endS) {
    errors.push({ name: 'endS', message: 'Конец периода должен быть позже начала' })
  }
  if (
    state.satelliteId && Number.isFinite(state.startS) && Number.isFinite(state.endS) && state.startS < state.endS
    && isDuplicateFailure({ satellite_id: state.satelliteId, start_s: Math.round(state.startS), end_s: Math.round(state.endS) })
  ) {
    errors.push({ name: 'endS', message: 'Такой период недоступности уже есть в списке' })
  }
  return errors
}

// ── периоды недоступности: сохранённые + черновые добавления/удаления одной таблицей ──────────

const failureRows = computed<FailureRow[]>(() => {
  const removedKeys = new Set(pendingRemove.value.map(failureKeyOf))
  const existingRows: FailureRow[] = (committedScenario.value?.failures ?? []).map(f => ({
    satelliteId: f.satellite_id,
    startS: f.start_s,
    endS: f.end_s,
    durationS: f.end_s - f.start_s,
    status: removedKeys.has(failureKeyOf(f)) ? 'pending-remove' : 'existing',
    source: f
  }))
  const addedRows: FailureRow[] = pendingAdd.value.map(f => ({
    satelliteId: f.satellite_id,
    startS: f.start_s,
    endS: f.end_s,
    durationS: f.end_s - f.start_s,
    status: 'pending-add',
    source: f
  }))
  return [...existingRows, ...addedRows]
    .sort((a, b) => a.satelliteId.localeCompare(b.satelliteId) || a.startS - b.startS)
})

const failureColumns: TableColumn<FailureRow>[] = [
  { accessorKey: 'satelliteId', header: 'Аппарат' },
  { id: 'startS', header: 'Начало' },
  { id: 'endS', header: 'Конец' },
  { id: 'durationS', header: 'Длительность' },
  { id: 'status', header: 'Статус' },
  { id: 'actions', header: '' }
]

const failuresSummaryText = computed(() => {
  const bits = [`сохранено: ${committedScenario.value?.failures.length ?? 0}`]
  if (pendingAdd.value.length) bits.push(`к добавлению: +${pendingAdd.value.length}`)
  if (pendingRemove.value.length) bits.push(`к удалению: −${pendingRemove.value.length}`)
  return bits.join(' · ')
})

function addFailure(): void {
  const entry: FailureEntry = {
    satellite_id: newFailure.satelliteId as string,
    start_s: Math.round(newFailure.startS),
    end_s: Math.round(newFailure.endS)
  }
  pendingAdd.value.push(entry)
  newFailure.satelliteId = undefined
  newFailure.startS = 0
  newFailure.endS = Math.min(3600, horizonS.value)
}

function removeExisting(f: FailureEntry): void {
  if (!pendingRemove.value.some(x => failureKeyOf(x) === failureKeyOf(f))) {
    pendingRemove.value.push(f)
  }
}
function restoreExisting(f: FailureEntry): void {
  pendingRemove.value = pendingRemove.value.filter(x => failureKeyOf(x) !== failureKeyOf(f))
}
function cancelPendingAdd(f: FailureEntry): void {
  pendingAdd.value = pendingAdd.value.filter(x => x !== f)
}

function formatHm(totalSeconds: number): string {
  const clamped = Math.max(0, Math.round(totalSeconds))
  const h = Math.floor(clamped / 3600)
  const m = Math.floor((clamped % 3600) / 60)
  return `${h} ч ${String(m).padStart(2, '0')} мин`
}

// ── сохранённые варианты: сохранение, возврат, «новый запуск» ─────────────────────────────────

const savedColumns: TableColumn<SavedVariant>[] = [
  { id: 'label', header: 'Вариант' },
  { id: 'params', header: 'Параметры' },
  { id: 'savedAt', header: 'Сохранён' },
  { id: 'actions', header: '' }
]

function shortId(id: string): string {
  return id.length > 10 ? `${id.slice(0, 10)}…` : id
}

/** Отправляет накопленную дельту черновика на сервер (`scenario.patch`) и делает результат новой
 *  базой. Возвращает `null`, если сохранять нечего (дизейбл кнопки это уже не пускает, но метод
 *  переиспользуется и из выгрузки, где до вызова ещё нет гарантии) или сервер отклонил патч —
 *  сообщение об ошибке в этом случае уже показано тостом внутри `useWs().request`. */
async function commitDraft(label: string): Promise<SavedVariant | null> {
  const baseVariantId = committedVariantId.value
  if (!baseVariantId) return null
  const patch = buildPatch()
  if (Object.keys(patch).length === 0) return null

  saving.value = true
  try {
    const result = await request<ScenarioCommitPayload>('scenario.patch', { variant_id: baseVariantId, ...patch })
    const entry: SavedVariant = {
      variantId: result.variant_id,
      label: label || describeChanges(patch),
      summary: result.summary,
      effectiveScenario: result.effective_scenario,
      savedAt: new Date()
    }
    adoptCommitted(result.variant_id, result.effective_scenario, result.summary)
    savedVariants.value.unshift(entry)
    emit('variant-changed', { variantId: result.variant_id, summary: result.summary })
    return entry
  } catch {
    return null
  } finally {
    saving.value = false
  }
}

async function onSave(_event: FormSubmitEvent<DraftState>): Promise<void> {
  const entry = await commitDraft(saveLabel.value.trim())
  if (entry) {
    toast.add({ title: 'Вариант сохранён', description: entry.label, color: 'success', icon: 'i-lucide-save' })
  }
}

function resetChanges(): void {
  resetDraft()
  toast.add({
    title: 'Изменения сброшены',
    description: 'Форма возвращена к последнему сохранённому варианту.',
    color: 'neutral',
    icon: 'i-lucide-rotate-ccw'
  })
}

function newLaunch(): void {
  if (!originalVariantId.value || !originalScenario.value) return
  adoptCommitted(originalVariantId.value, originalScenario.value, originalSummary.value)
  emit('variant-changed', { variantId: originalVariantId.value, summary: originalSummary.value ?? {} })
  toast.add({
    title: 'Новый запуск',
    description: 'Конфигурация возвращена к исходно загруженному сценарию. Сохранённые варианты остались в списке ниже.',
    color: 'neutral',
    icon: 'i-lucide-rocket'
  })
}

async function returnToSaved(entry: SavedVariant): Promise<void> {
  adoptCommitted(entry.variantId, entry.effectiveScenario, entry.summary)
  emit('variant-changed', { variantId: entry.variantId, summary: entry.summary })
  toast.add({ title: 'Вариант восстановлен', description: entry.label, color: 'success', icon: 'i-lucide-history' })
}

function downloadJson(doc: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/** Отдельная выгрузка изменённого сценария `cosmo-A-1.0` — НЕ выгрузка результата расчёта
 *  (`cosmo-A-result-1.0`, `.claude/rules/protocol.md`, `export.kind==='scenario'`). Сервер
 *  экспортирует только то, что уже знает по `variant_id`, поэтому несохранённый черновик перед
 *  выгрузкой сначала молча фиксируется тем же путём, что и «Сохранить вариант». */
async function exportScenario(): Promise<void> {
  if (!committedVariantId.value) return
  let variantId = committedVariantId.value
  if (isDirty.value) {
    const entry = await commitDraft(saveLabel.value.trim())
    if (!entry) return
    variantId = entry.variantId
  }
  exporting.value = true
  try {
    const doc = await request<EffectiveScenario>('export', { variant_id: variantId, kind: 'scenario' })
    const idPart = typeof doc.meta?.id === 'string' && doc.meta.id ? doc.meta.id : 'scenario'
    downloadJson(doc, `${idPart}.${shortId(variantId)}.cosmo-A-1.0.json`)
    toast.add({
      title: 'Сценарий выгружен',
      description: 'Формат cosmo-A-1.0 — файл пригоден для повторной загрузки в сервис.',
      color: 'success',
      icon: 'i-lucide-download'
    })
  } catch {
    // Ошибка протокола уже показана тостом внутри useWs().request.
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <div class="h-full overflow-y-auto p-4">
    <UEmpty
      v-if="!committedScenario"
      icon="i-lucide-sliders-horizontal"
      title="Конфигурация недоступна"
      description="Сначала загрузите сценарий cosmo-A-1.0 в боковой панели — здесь появятся очередь запуска, плоскости и периоды недоступности аппаратов."
    />

    <div v-else class="flex flex-col gap-5 max-w-3xl">
      <div class="flex flex-wrap items-center gap-2">
        <UBadge :label="`Текущий вариант: ${shortId(committedVariantId!)}`" color="neutral" variant="subtle" icon="i-lucide-git-branch" />
        <UBadge v-if="isDirty" label="Есть несохранённые изменения" color="warning" variant="subtle" icon="i-lucide-pencil" />
        <UBadge v-if="savedVariants.length" :label="`Сохранённых вариантов: ${savedVariants.length}`" color="neutral" variant="subtle" icon="i-lucide-history" />
      </div>

      <UForm :state="draft" :validate="validateDraft" class="flex flex-col gap-5" @submit="onSave">
        <UCard>
          <template #header>
            <h3 class="font-semibold text-highlighted">Очередь запуска</h3>
          </template>
          <UFormField
            name="launchStage"
            help="Меняет, какие аппараты активны (`launch_batch ≤ launch_stage`) — влияет на видимость и маршруты сразу для всех пунктов."
          >
            <URadioGroup v-model="draft.launchStage" :items="launchStageItems" variant="card" />
          </UFormField>
        </UCard>

        <UCard>
          <template #header>
            <h3 class="font-semibold text-highlighted">Орбитальные плоскости</h3>
          </template>
          <div class="grid gap-4 sm:grid-cols-2">
            <div v-for="plane in planeList" :key="plane.id" class="rounded-md ring ring-default p-3 flex flex-col gap-3">
              <p class="text-sm font-semibold text-highlighted">{{ plane.id }}</p>
              <UFormField label="RAAN, °" :name="`planes.${plane.id}.raan_deg`">
                <UInputNumber
                  v-model="draft.planes[plane.id]!.raan_deg"
                  :min="0"
                  :max="359.999"
                  :step="0.5"
                  :step-snapping="false"
                  class="w-full"
                />
              </UFormField>
              <UFormField label="Фаза, °" :name="`planes.${plane.id}.phase_deg`">
                <UInputNumber
                  v-model="draft.planes[plane.id]!.phase_deg"
                  :min="0"
                  :max="359.999"
                  :step="0.5"
                  :step-snapping="false"
                  class="w-full"
                />
              </UFormField>
            </div>
          </div>
        </UCard>

        <UFormField label="Название варианта (необязательно)" description="Если оставить пустым — сформируем из изменённых параметров.">
          <UInput v-model="saveLabel" placeholder="например, «RAAN 0/50/100»" icon="i-lucide-tag" class="w-full" />
        </UFormField>

        <div class="flex flex-wrap gap-2">
          <UButton type="submit" label="Сохранить вариант" icon="i-lucide-save" :loading="saving" :disabled="!isDirty || busy" />
          <UButton
            type="button"
            label="Сбросить изменения"
            icon="i-lucide-rotate-ccw"
            color="neutral"
            variant="outline"
            :disabled="!isDirty || busy"
            @click="resetChanges"
          />
          <UTooltip text="Вернуться к исходно загруженному сценарию, отменив патчи этой сессии. Сохранённые варианты останутся в списке ниже.">
            <UButton
              type="button"
              label="Новый запуск"
              icon="i-lucide-rocket"
              color="neutral"
              variant="ghost"
              :disabled="!canStartOver || busy"
              @click="newLaunch"
            />
          </UTooltip>
          <UTooltip text="Выгрузка изменённого сценария в формате cosmo-A-1.0 — отдельно от выгрузки результата расчёта.">
            <UButton
              type="button"
              label="Выгрузить изменённый сценарий"
              icon="i-lucide-download"
              color="neutral"
              variant="subtle"
              :loading="exporting"
              :disabled="busy"
              @click="exportScenario"
            />
          </UTooltip>
        </div>
      </UForm>

      <USeparator />

      <div class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="font-semibold text-highlighted">Периоды недоступности аппаратов</h3>
          <p class="text-sm text-muted">{{ failuresSummaryText }}</p>
        </div>
        <UButton label="Управлять" icon="i-lucide-list-x" color="neutral" variant="outline" @click="isOutagesOpen = true" />
      </div>

      <USeparator />

      <div>
        <h3 class="font-semibold text-highlighted mb-2">Сохранённые варианты</h3>
        <div class="overflow-x-auto rounded-md ring ring-default">
          <UTable
            :data="savedVariants"
            :columns="savedColumns"
            empty="Пока нет сохранённых вариантов — сохраните текущую конфигурацию, чтобы вернуться к ней позже."
          >
            <template #label-cell="{ row }">
              <div class="flex items-center gap-2">
                <span class="font-medium text-highlighted">{{ row.original.label }}</span>
                <UBadge v-if="row.original.variantId === committedVariantId" label="текущий" color="primary" variant="subtle" size="sm" />
              </div>
              <p class="text-xs text-dimmed font-mono">{{ shortId(row.original.variantId) }}</p>
            </template>
            <template #params-cell="{ row }">
              <span class="text-sm text-muted">{{ describeSummary(row.original.summary) }}</span>
            </template>
            <template #savedAt-cell="{ row }">
              <span class="text-sm text-muted">{{ row.original.savedAt.toLocaleTimeString('ru-RU') }}</span>
            </template>
            <template #actions-cell="{ row }">
              <UButton
                label="Вернуться"
                icon="i-lucide-undo-2"
                size="xs"
                color="neutral"
                variant="outline"
                :disabled="row.original.variantId === committedVariantId"
                @click="returnToSaved(row.original)"
              />
            </template>
          </UTable>
        </div>
      </div>

      <USlideover
        v-model:open="isOutagesOpen"
        title="Периоды недоступности аппаратов"
        description="Отказавший аппарат сохраняет положение на орбите, но выпадает из всех связей на время периода — интервал полуоткрытый [start_s, end_s)."
      >
        <template #body>
          <div class="flex flex-col gap-5">
            <UForm
              :state="newFailure"
              :validate="validateNewFailure"
              class="flex flex-col gap-3 sm:flex-row sm:items-end sm:flex-wrap"
              @submit="addFailure"
            >
              <UFormField label="Аппарат" name="satelliteId" class="flex-1 min-w-40">
                <USelect v-model="newFailure.satelliteId" :items="satelliteOptions" placeholder="ID аппарата" class="w-full" />
              </UFormField>
              <UFormField label="Начало, с" name="startS" class="w-32">
                <UInputNumber v-model="newFailure.startS" :min="0" :max="horizonS" :step="60" :step-snapping="false" class="w-full" />
              </UFormField>
              <UFormField label="Конец, с" name="endS" class="w-32">
                <UInputNumber v-model="newFailure.endS" :min="0" :max="horizonS" :step="60" :step-snapping="false" class="w-full" />
              </UFormField>
              <UButton type="submit" label="Добавить" icon="i-lucide-plus" color="neutral" variant="subtle" />
            </UForm>

            <USeparator />

            <div class="overflow-x-auto rounded-md ring ring-default">
              <UTable :data="failureRows" :columns="failureColumns" empty="Периодов недоступности нет.">
                <template #startS-cell="{ row }">
                  <p>{{ formatHm(row.original.startS) }}</p>
                  <p class="text-xs text-dimmed">{{ row.original.startS }} с</p>
                </template>
                <template #endS-cell="{ row }">
                  <p>{{ formatHm(row.original.endS) }}</p>
                  <p class="text-xs text-dimmed">{{ row.original.endS }} с</p>
                </template>
                <template #durationS-cell="{ row }">
                  {{ formatHm(row.original.durationS) }}
                </template>
                <template #status-cell="{ row }">
                  <UBadge v-if="row.original.status === 'existing'" label="сохранено" color="neutral" variant="subtle" />
                  <UBadge v-else-if="row.original.status === 'pending-add'" label="новое" color="success" variant="subtle" />
                  <UBadge v-else label="к удалению" color="error" variant="subtle" />
                </template>
                <template #actions-cell="{ row }">
                  <UButton
                    v-if="row.original.status === 'existing'"
                    label="Удалить"
                    color="error"
                    variant="ghost"
                    size="xs"
                    icon="i-lucide-trash-2"
                    @click="removeExisting(row.original.source)"
                  />
                  <UButton
                    v-else-if="row.original.status === 'pending-remove'"
                    label="Восстановить"
                    color="neutral"
                    variant="ghost"
                    size="xs"
                    icon="i-lucide-undo-2"
                    @click="restoreExisting(row.original.source)"
                  />
                  <UButton
                    v-else
                    label="Отменить"
                    color="neutral"
                    variant="ghost"
                    size="xs"
                    icon="i-lucide-x"
                    @click="cancelPendingAdd(row.original.source)"
                  />
                </template>
              </UTable>
            </div>

            <UAlert
              v-if="pendingAdd.length || pendingRemove.length"
              color="warning"
              variant="subtle"
              icon="i-lucide-circle-alert"
              title="Правки периодов недоступности ещё не сохранены"
              description="Нажмите «Сохранить вариант» на основной панели, чтобы применить их вместе с остальной формой."
            />
          </div>
        </template>
        <template #footer>
          <UButton label="Готово" color="primary" @click="isOutagesOpen = false" />
        </template>
      </USlideover>
    </div>
  </div>
</template>
