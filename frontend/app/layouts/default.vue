<script setup lang="ts">
/**
 * Каркас рабочего экрана: `UDashboardGroup` + `UDashboardSidebar`, собран по `.claude/rules/frontend.md`.
 * Сайдбар отвечает за загрузку сценария (`UFileUpload`, ошибки — `UAlert`), основная панель —
 * за визуализацию, её собирает `pages/index.vue` в слоте `<slot />`.
 */

interface SampleEntry {
  name: string
  size_bytes: number
  url: string
}

const config = useRuntimeConfig()
const apiBase = config.public.apiUrl as string
const wsStatus = useWs().status
const { variantId, sourceName, loading, error, loadFromRawJson } = useScenario()

const scenarioFile = ref<File | null>(null)
const samples = ref<SampleEntry[]>([])
const samplesLoading = ref(false)

function apiUrl(path: string): string {
  return path.startsWith('http') ? path : `${apiBase}${path}`
}

async function fetchSamples(): Promise<void> {
  samplesLoading.value = true
  try {
    samples.value = await $fetch<SampleEntry[]>(apiUrl('/samples'))
  } catch {
    // `/samples` — вспомогательная ручка для демо-сценариев, а не расчётный путь (см.
    // `backend/app/main.py`): если она недоступна, просто не показываем список примеров,
    // загрузка своего файла продолжает работать.
    samples.value = []
  } finally {
    samplesLoading.value = false
  }
}
onMounted(fetchSamples)

watch(scenarioFile, async (file) => {
  if (!file) return
  const text = await file.text()
  await loadFromRawJson(text, file.name)
})

async function loadSample(sample: SampleEntry): Promise<void> {
  const text = await $fetch<string>(apiUrl(sample.url), { responseType: 'text' })
  await loadFromRawJson(text, sample.name)
}

const wsStatusBadge = computed(() => {
  switch (wsStatus.value) {
    case 'open':
      return { label: 'Соединение установлено', color: 'success' as const, icon: 'i-lucide-circle-check' }
    case 'connecting':
      return { label: 'Подключение…', color: 'warning' as const, icon: 'i-lucide-loader-circle' }
    default:
      return { label: 'Нет соединения', color: 'error' as const, icon: 'i-lucide-circle-x' }
  }
})
</script>

<template>
  <UDashboardGroup unit="rem">
    <UDashboardSidebar collapsible resizable :min-size="16" :default-size="19" :max-size="26">
      <template #header="{ collapsed }">
        <UIcon name="i-lucide-satellite" class="size-5 text-primary shrink-0" />
        <span v-if="!collapsed" class="font-semibold text-highlighted truncate">Спутниковая группировка</span>
      </template>

      <template #default="{ collapsed }">
        <template v-if="!collapsed">
          <div class="flex flex-col gap-2">
            <h3 class="text-xs font-medium text-muted">Сценарий</h3>

            <UFileUpload
              v-model="scenarioFile"
              accept="application/json,.json"
              icon="i-lucide-file-json"
              label="Перетащите файл сценария"
              description="cosmo-A-1.0, JSON"
              :disabled="loading"
              class="min-h-32"
            />

            <UAlert
              v-if="error"
              color="error"
              variant="subtle"
              icon="i-lucide-circle-alert"
              :title="error.field ? `Ошибка: ${error.field}` : `Ошибка (${error.code})`"
              :description="error.message"
            />

            <UAlert
              v-else-if="variantId"
              color="success"
              variant="subtle"
              icon="i-lucide-circle-check"
              title="Сценарий загружен"
              :description="`${sourceName} · variant_id: ${variantId}`"
            />
          </div>

          <div v-if="samplesLoading || samples.length" class="flex flex-col gap-2 mt-4">
            <h3 class="text-xs font-medium text-muted">Примеры из «Данные/»</h3>
            <USkeleton v-if="samplesLoading" class="h-8 w-full" />
            <UButton
              v-for="sample in samples"
              :key="sample.name"
              :label="sample.name"
              icon="i-lucide-folder-open"
              color="neutral"
              variant="outline"
              block
              :loading="loading"
              @click="loadSample(sample)"
            />
          </div>
        </template>

        <template v-else>
          <UTooltip text="Загрузка сценария">
            <UButton icon="i-lucide-file-json" color="neutral" variant="ghost" square />
          </UTooltip>
        </template>
      </template>

      <template #footer="{ collapsed }">
        <UTooltip :text="wsStatusBadge.label">
          <UBadge
            :color="wsStatusBadge.color"
            variant="subtle"
            :icon="wsStatusBadge.icon"
            :label="collapsed ? undefined : wsStatusBadge.label"
            :square="collapsed"
            class="w-full justify-center"
          />
        </UTooltip>
        <UColorModeButton v-if="!collapsed" />
      </template>
    </UDashboardSidebar>

    <slot />
  </UDashboardGroup>
</template>
