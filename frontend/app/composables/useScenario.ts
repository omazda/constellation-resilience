/**
 * Состояние текущего загруженного сценария — одно на приложение: сайдбар грузит файл или пример,
 * основная панель читает `variantId`/`summary`, дальше на него же будут опираться `snapshot` и
 * `compute` (контракт — `.claude/rules/protocol.md`). Сам запрос — `scenario.load` через
 * `useWs()`; здесь только состояние и разбор файла, транспорт не дублируется.
 */
import type { WsErrorPayload } from './useWs'

/** Форма ответа `scenario.load` зафиксирована в контракте только частично — `variant_id` назван
 *  дословно в таблице `.claude/rules/protocol.md`, остальные поля («нормализованный сценарий,
 *  сводка») до появления `docs/PROTOCOL.md` не типизируем строже, чтобы не выдумывать контракт. */
export interface ScenarioLoadResult {
  variant_id: string
  [key: string]: unknown
}

const variantId = ref<string | null>(null)
const summary = ref<ScenarioLoadResult | null>(null)
const sourceName = ref<string | null>(null)
const loading = ref(false)
const error = ref<WsErrorPayload | null>(null)

/** Разбирает сырой JSON сценария и загружает его через `scenario.load`. `label` — имя файла или
 *  примера, идёт в сообщение об ошибке разбора и в поле `field`, если ошибка серверная не назвала
 *  своё (запасной вариант — сам JSON синтаксически невалиден раньше, чем дошёл до валидатора). */
async function loadFromRawJson(raw: string, label: string): Promise<void> {
  error.value = null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    error.value = {
      code: 'client.invalid_json',
      field: label,
      message: 'Файл не является корректным JSON и не был отправлен на сервер.'
    }
    return
  }

  const { request, detachVariant } = useWs()
  loading.value = true
  try {
    const result = await request<ScenarioLoadResult>('scenario.load', parsed)
    if (variantId.value) detachVariant(variantId.value)
    variantId.value = result.variant_id
    summary.value = result
    sourceName.value = label
  } catch (caught) {
    error.value = caught instanceof WsTransportError
      ? { code: 'transport', field: null, message: caught.message }
      : (caught as WsErrorPayload)
  } finally {
    loading.value = false
  }
}

export function useScenario() {
  return {
    variantId: readonly(variantId),
    summary: readonly(summary),
    sourceName: readonly(sourceName),
    loading: readonly(loading),
    error: readonly(error),
    loadFromRawJson
  }
}
