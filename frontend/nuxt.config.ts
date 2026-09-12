// Конфигурация Nuxt-приложения. Режим — чистый SPA: сервис отдаётся жюри одним контейнером,
// где FastAPI раздаёт статику из `nuxt generate` (`.output/public` -> `backend/static`,
// см. .claude/rules/docs.md, Dockerfile) и SPA-fallback на `200.html`. Серверный рендер тут не
// нужен и не проверяется критериями — поэтому `ssr: false`.
export default defineNuxtConfig({
  compatibilityDate: '2025-09-01',
  ssr: false,
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],
  devtools: { enabled: true },

  // Консоль рассчитана на тёмную подложку: ультрафиолет на светлом теряет и контраст, и смысл
  // (свечение перестаёт кодировать состояние). Переключатель в сайдбаре остаётся — тема
  // навязана как значение по умолчанию, а не намертво.
  colorMode: { preference: 'dark', fallback: 'dark' },

  runtimeConfig: {
    public: {
      // Адрес WS-эндпоинта, если фронтенд и бэкенд не на одном origin — например, `nuxt dev`
      // поднят на 3000, а бэкенд отдельно на 8000 (см. .claude/rules/protocol.md, CORS разрешён
      // именно для этого случая). Пусто -> берём текущий origin (прод-сборка в одном контейнере,
      // .claude/rules/docs.md). Переопределяется переменной окружения `NUXT_PUBLIC_WS_URL`.
      wsUrl: '',
      // То же самое, но для единственных REST-ручек бэкенда (`/health`, `/samples*`,
      // см. `backend/app/main.py`) — расчёт по-прежнему целиком идёт через `/ws`.
      // Переопределяется `NUXT_PUBLIC_API_URL`.
      apiUrl: ''
    }
  },

  app: {
    head: {
      title: 'Проектирование спутниковой группировки',
      htmlAttrs: { lang: 'ru' },
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }]
    }
  },

  typescript: {
    typeCheck: false // прогоняется отдельно командой `npm run typecheck`, не блокирует dev/build
  }
})
