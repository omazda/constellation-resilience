/**
 * Единственное WS-соединение с бэкендом на весь SPA. Контракт — `docs/PROTOCOL.md`:
 * конверт `{id, type, payload}`, ответ несёт тот же `id`; ошибка — `{id, type:"error", code,
 * field, message}`. REST-ручек, кроме `/health` и раздачи сэмплов, нет — весь расчёт идёт через
 * `/ws`.
 *
 * Состояние (сокет, очередь запросов, список привязанных вариантов) хранится на уровне модуля,
 * а не внутри `useWs()`: соединение должно быть одно на приложение, а не одно на компонент,
 * который его запросил.
 */

export type WsStatus = 'connecting' | 'open' | 'closed'

/** Известные типы запросов клиента, см. таблицу в `docs/PROTOCOL.md`. */
export type WsRequestType =
  | 'scenario.load'
  | 'scenario.patch'
  | 'compute'
  | 'snapshot'
  | 'compare'
  | 'export'
  | 'attach'

/** Тело ошибки протокола — единственная часть конверта, чья форма зафиксирована дословно. */
export interface WsErrorPayload {
  code: string
  field?: string | null
  message: string
}

/** Ошибка транспорта (обрыв соединения до ответа) — отличаем от `WsErrorPayload` сервера. */
export class WsTransportError extends Error {}

interface WsEnvelope {
  id: string
  type: string
  payload: unknown
}

interface PendingRequest {
  resolve: (payload: unknown) => void
  reject: (error: Error | WsErrorPayload) => void
  /** Для `compute`: сервер шлёт серию `progress` с тем же `id`, промис остаётся открытым. */
  onProgress?: (payload: unknown) => void
}

const RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000, 8000, 15_000] // растущая пауза, затем полка

const status = ref<WsStatus>('closed')
const pending = new Map<string, PendingRequest>()
/** Конверты, которые попросили отправить до того, как сокет открылся. */
const outbox: string[] = []
/** `variant_id` -> последний результат `attach`, обновляется автоматически при реконнекте. */
const attachedResults = reactive(new Map<string, Ref<unknown>>())

/** Манифест `compute`/`attach`, который ждёт свой бинарный фрейм. У фрейма нет своего `id`,
 *  связь только по порядку прихода — поэтому состояние одно, а не словарь по `id`. */
let awaitingBinary: { id: string; entry: PendingRequest; manifest: Record<string, unknown> } | null = null

let socket: WebSocket | null = null
let reconnectAttempt = 0
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let cachedWsUrl: string | undefined
let connectStarted = false

function resolveWsUrl(): string {
  if (cachedWsUrl) return cachedWsUrl
  // useRuntimeConfig читаем один раз при первом подключении — на этот момент composable всегда
  // вызван из-под контекста Nuxt-приложения (тело `useWs()` при инициализации компонента).
  const override = useRuntimeConfig().public.wsUrl as string
  if (override) {
    cachedWsUrl = override
  } else {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    cachedWsUrl = `${proto}//${window.location.host}/ws`
  }
  return cachedWsUrl
}

function generateId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** Сообщение об ошибке протокола тостом, с именем проблемного поля в заголовке — ТЗ требует
 *  понятных сообщений о невалидном вводе, а не молчаливого отказа. */
function notifyError(error: WsErrorPayload): void {
  const toast = useToast()
  toast.add({
    title: error.field ? `Ошибка: ${error.field}` : `Ошибка (${error.code})`,
    description: error.message,
    color: 'error',
    icon: 'i-lucide-circle-alert'
  })
}

function flushOutbox(): void {
  if (!socket || socket.readyState !== WebSocket.OPEN) return
  while (outbox.length > 0) {
    socket.send(outbox.shift() as string)
  }
}

/** После (пере)подключения просим сервер вернуть готовый пакет по каждому варианту, за которым
 *  следили — это и есть «повторная привязка через attach» вместо пересчёта. */
function reattachAll(): void {
  for (const [variantId, resultRef] of attachedResults) {
    void requestAttach(variantId, resultRef)
  }
}

async function requestAttach(variantId: string, resultRef: Ref<unknown>): Promise<void> {
  try {
    resultRef.value = await request('attach', { variant_id: variantId })
  } catch {
    // Ошибка уже показана тостом внутри request(); значение просто останется прежним.
  }
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return
  const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)]
  reconnectAttempt += 1
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    openSocket()
  }, delay)
}

function rejectAllPending(reason: Error): void {
  // Обрыв между манифестом и его фреймом оставил бы промис висеть навсегда.
  awaitingBinary = null
  for (const [id, entry] of pending) {
    entry.reject(reason)
    pending.delete(id)
  }
}

function openSocket(): void {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return
  }
  status.value = 'connecting'
  const ws = new WebSocket(resolveWsUrl())
  // Пакет `compute`/`attach` приходит бинарным фреймом сразу следом за манифестом
  // (docs/PROTOCOL.md). Без этого браузер отдал бы его Blob-ом и типизированные массивы
  // пришлось бы читать асинхронно.
  ws.binaryType = 'arraybuffer'
  socket = ws

  ws.addEventListener('open', () => {
    status.value = 'open'
    reconnectAttempt = 0
    flushOutbox()
    reattachAll()
  })

  ws.addEventListener('message', (event) => {
    if (typeof event.data !== 'string') {
      // Бинарный фрейм всегда идёт СРАЗУ следом за манифестом с `binary: true` и относится
      // к нему (docs/PROTOCOL.md, раздел «compute / attach»). Собственного `id` у фрейма нет,
      // поэтому связываем по порядку прихода.
      if (!awaitingBinary) {
        console.warn('useWs: бинарный фрейм без манифеста — пропущен')
        return
      }
      const { entry, manifest, id } = awaitingBinary
      awaitingBinary = null
      entry.resolve({ ...manifest, buffer: event.data as ArrayBuffer })
      pending.delete(id)
      return
    }

    let envelope: WsEnvelope
    try {
      envelope = JSON.parse(event.data) as WsEnvelope
    } catch {
      console.error('useWs: ответ сервера не является JSON', event.data)
      return
    }

    if (envelope.type === 'error') {
      const error = envelope.payload as WsErrorPayload
      notifyError(error)
      const entry = pending.get(envelope.id)
      if (entry) {
        entry.reject(error)
        pending.delete(envelope.id)
      }
      return
    }

    const entry = pending.get(envelope.id)
    if (!entry) return // ответ на запрос, которого уже никто не ждёт (например, после reload)

    if (envelope.type === 'progress' && entry.onProgress) {
      entry.onProgress(envelope.payload)
      return // промис остаётся открытым до финального пакета с тем же id
    }

    // Манифест расчёта разрешает промис не сам по себе: за ним обязан прийти бинарный фрейм,
    // и только вместе они составляют пакет результата.
    const payload = envelope.payload as { binary?: boolean } | null
    if (payload && typeof payload === 'object' && payload.binary === true) {
      awaitingBinary = { id: envelope.id, entry, manifest: payload }
      return
    }

    entry.resolve(envelope.payload)
    pending.delete(envelope.id)
  })

  ws.addEventListener('close', () => {
    status.value = 'closed'
    socket = null
    rejectAllPending(new WsTransportError('Соединение с сервером разорвано'))
    scheduleReconnect()
  })

  ws.addEventListener('error', () => {
    // Реальную причину сообщит следующее событие close — здесь просто не даём ошибке остаться
    // необработанной в консоли браузера.
  })
}

function connect(): void {
  if (connectStarted) return
  connectStarted = true
  openSocket()
}

/**
 * Отправляет запрос и возвращает промис с телом ответа. `id` генерируется и сопоставляется
 * с ответом автоматически; ошибка протокола отклоняет промис телом `WsErrorPayload` (и уже
 * показана тостом), обрыв соединения — `WsTransportError`.
 *
 * `onProgress` — для `compute`: вызывается на каждое сообщение `type:"progress"` с тем же `id`,
 * промис разрешается только финальным пакетом.
 */
function request<TResult = unknown>(
  type: WsRequestType,
  payload: unknown = {},
  opts: { onProgress?: (payload: unknown) => void } = {}
): Promise<TResult> {
  connect()
  const id = generateId()
  const envelope: WsEnvelope = { id, type, payload }
  const json = JSON.stringify(envelope)

  return new Promise<TResult>((resolve, reject) => {
    pending.set(id, {
      resolve: resolve as (payload: unknown) => void,
      reject,
      onProgress: opts.onProgress
    })
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(json)
    } else {
      outbox.push(json)
    }
  })
}

/**
 * Регистрирует `variant_id` для автоматической повторной привязки после реконнекта и сразу
 * запрашивает текущий пакет результата. Возвращает реактивную ссылку, которая сама обновится
 * при следующем `attach` (в т.ч. после обрыва связи) — компонент может просто следить за ней.
 */
function attachVariant(variantId: string): Ref<unknown> {
  let resultRef = attachedResults.get(variantId)
  if (!resultRef) {
    resultRef = ref(null)
    attachedResults.set(variantId, resultRef)
  }
  void requestAttach(variantId, resultRef)
  return resultRef
}

/** Прекращает следить за вариантом (например, после загрузки нового сценария поверх старого). */
function detachVariant(variantId: string): void {
  attachedResults.delete(variantId)
}

export function useWs() {
  connect()
  return {
    status: readonly(status),
    request,
    attachVariant,
    detachVariant
  }
}
