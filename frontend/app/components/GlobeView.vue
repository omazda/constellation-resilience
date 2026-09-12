<script setup lang="ts">
/**
 * 3D-глобус спутниковой группировки на `globe.gl` (Three.js/WebGL) — .claude/rules/frontend.md.
 *
 * Ничего не считает: спутники, рёбра сети и маршруты приходят уже готовыми снимком `snapshot`
 * (.claude/rules/protocol.md, `{variant_id, t_s, satellites, isl_edges, ground_contacts, routes}`),
 * а координаты наземных пунктов — прямо из `ground_sites` сценария. Единственное исключение —
 * радиус кольца «зона видимости»: это статическая элевационная маска (сферическая тригонометрия
 * по `altitude_km`/`min_elevation_deg`, две скалярные величины из `environment`), а не орбитальная
 * механика `geometry.py` — она не входит в расчёт доступности и нужна только для подсказки на карте.
 *
 * Слои (по одному вызову `*Data` на layout-обновление, без пересоздания WebGL-контекста):
 *   pointsData — аппараты (неактивные серым, pointLabel — id + статус);
 *   arcsData   — совмещённый массив ISL-линий, наземных контактов и сегментов выбранного маршрута
 *                (у globe.gl один слой дуг на инстанс; второй `new Globe()` ради второго набора
 *                дуг завёл бы второй WebGL-контекст — ровно та ловушка, от которой предостерегает
 *                правило ниже, — поэтому маршрут отличают полем `kind` и своим стилем/`arcDashAnimateTime`,
 *                а не отдельным инстансом);
 *   labelsData — наземные пункты и шлюз, цвет точки — есть ли у пункта маршрут прямо сейчас;
 *   ringsData  — зона видимости вокруг спутников, которые сейчас кого-то обслуживают.
 */
import Globe from 'globe.gl'
import type { GlobeInstance } from 'globe.gl'

// ─────────────────────────── контракт данных снимка сети ────────────────────────────
// Формы полей — как отдаёт `snapshot` (backend/app/ws.py:_handle_snapshot) и
// `effective_scenario.ground_sites` (backend/core/scenario.py); значения не переобрабатываются.

export interface GlobeSatellite {
  id: string
  lat_deg: number
  lon_deg: number
  /** (r − R) / R — уже в радиусах Земли, ровно единица измерения `pointAltitude` у globe.gl. */
  alt_ratio: number
  active: boolean
}

export interface GlobeIslEdge {
  a: string
  b: string
  /** Необязательно: бинарный пакет расчёта отдаёт только индексы концов ребра, без расстояния —
   *  оно нужно лишь подсказке при наведении. */
  dist_km?: number
}

export interface GlobeGroundContact {
  ground_id: string
  satellite_id: string
  dist_km?: number
  elevation_deg?: number | null
}

export interface GlobeGroundSite {
  id: string
  name: string
  role: 'client' | 'gateway'
  lat_deg: number
  lon_deg: number
}

/** Ровно четыре причины отсутствия маршрута — core.routing.REASONS, docs/PARAMETERS.md §6. */
export type GlobeRouteReason = 'no_visible_satellite' | 'isl_partition' | 'no_gateway_contact' | 'gateway_offline'

export interface GlobeRoute {
  client_id: string
  gateway_id: string | null
  /** Узлы маршрута client → …спутники… → gateway по порядку; пустой массив = маршрута нет. */
  path: string[]
  hops: number | null
  reason: GlobeRouteReason | null
}

const REASON_LABELS: Record<GlobeRouteReason, string> = {
  no_visible_satellite: 'Нет видимого спутника над пунктом в этот момент — проблема в покрытии.',
  isl_partition: 'Спутник виден, но межспутниковая сеть до шлюза разорвана.',
  no_gateway_contact: 'Есть связность до соседних спутников, но ни один не видит шлюз.',
  gateway_offline: 'Шлюз временно недоступен (интервал gateway_outages).'
}

const props = withDefaults(defineProps<{
  /** Аппараты снимка, включая неактивные (снимок отдаёт их все, не только видимых). */
  satellites: GlobeSatellite[]
  islEdges?: GlobeIslEdge[]
  groundContacts?: GlobeGroundContact[]
  /** `effective_scenario.ground_sites` — не зависит от t_s, передаётся отдельно от снимка. */
  groundSites: GlobeGroundSite[]
  /** Маршруты всех клиентских пунктов на этот t_s — не только выбранного. */
  routes?: GlobeRoute[]
  /** Чей маршрут подсвечен отдельным слоем дуг сверху; null — ничего не подсвечено. */
  selectedClientId?: string | null
  /** `environment.altitude_km` — только для радиуса кольца видимости, не для позиций. */
  altitudeKm?: number | null
  /** `environment.min_elevation_deg` — та же роль. */
  minElevationDeg?: number | null
  /** Подспутниковые следы за несколько предыдущих отсчётов: по неподвижной точке нельзя
   *  понять, куда летит аппарат, а на перемотке след превращает скачки в движение. */
  trails?: { id: string, points: [number, number, number][] }[]
  /** Идёт расчёт снимка — поверх глобуса показывается скелетон вместо пустой сцены. */
  loading?: boolean
}>(), {
  islEdges: () => [],
  groundContacts: () => [],
  routes: () => [],
  trails: () => [],
  selectedClientId: null,
  altitudeKm: null,
  minElevationDeg: null,
  loading: false
})

const emit = defineEmits<{
  /** Клик по подписи клиентского пункта — родитель решает, менять ли `selectedClientId`. */
  'select-client': [clientId: string]
}>()

// ────────────────────────────────── статические текстуры ──────────────────────────────────
// Бандл three-globe (MIT), скопированы в public/globe при сборке фронтенда — жюри открывает
// сервис без интернета (docs.md: образ обязан подниматься на машине без доступа к сети),
// поэтому никаких внешних CDN-текстур.
const EARTH_TEXTURE = '/globe/earth-dark.jpg'
const BUMP_TEXTURE = '/globe/earth-topology.png'
const STARFIELD_TEXTURE = '/globe/night-sky.png'

// ────────────────────────────────── цвета темы Nuxt UI ──────────────────────────────────
// `--ui-primary` и другие токены у Nuxt UI v4/Tailwind v4 заданы в цветовом пространстве oklch
// (tailwindcss/theme.css: `--color-sky-500: oklch(...)`), а разбор стиля у three.js
// (three/src/math/Color.js: Color.setStyle) понимает только rgb()/hsl()/hex/именованные цвета —
// oklch() ловит ветку `default: warn('Unknown color model')` и красит в чёрный (это же ловит и
// внутренний парсер цвета у three-globe, только вместо предупреждения падает с TypeError).
// Читать custom-property напрямую через getComputedStyle не помогает: вычисленное значение
// custom-property — просто текст после подстановки var(), браузер его не нормализует. Присвоить
// токен НАСТОЯЩЕМУ типизированному CSS-свойству (`color`) пробного элемента тоже недостаточно —
// современный Chrome (проверено рендером в реальном браузере, см. smoke-тест) сохраняет
// computed-значение color/background-color в исходном цветовом пространстве и отдаёт тот же
// oklch(...) текстом, а не понижает до rgb(). Рабочий способ — прогнать через canvas 2D:
// `fillStyle` разбирает ЛЮБОй синтаксис CSS-цвета (oklch/lab/color()/hex/rgb/hsl/имена), а
// `getImageData` всегда возвращает конкретные 0..255 sRGB-байты независимо от того, в каком
// пространстве был исходный цвет — вот такую строку `rgb(r, g, b)` three.js уже разбирает.
let colorProbeEl: HTMLElement | null = null
let colorCanvasCtx: CanvasRenderingContext2D | null = null
function ensureColorProbes(): void {
  if (!colorProbeEl) {
    colorProbeEl = document.createElement('span')
    colorProbeEl.style.position = 'fixed'
    colorProbeEl.style.opacity = '0'
    colorProbeEl.style.pointerEvents = 'none'
    colorProbeEl.setAttribute('aria-hidden', 'true')
    document.body.appendChild(colorProbeEl)
  }
  if (!colorCanvasCtx) {
    const canvas = document.createElement('canvas')
    canvas.width = 1
    canvas.height = 1
    colorCanvasCtx = canvas.getContext('2d', { willReadFrequently: true })
  }
}
function resolveThemeColor(varName: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  ensureColorProbes()
  if (!colorProbeEl || !colorCanvasCtx) return fallback

  colorProbeEl.style.color = `var(${varName})`
  const cssColor = getComputedStyle(colorProbeEl).color
  if (!cssColor) return fallback

  colorCanvasCtx.clearRect(0, 0, 1, 1)
  colorCanvasCtx.fillStyle = '#000000'
  colorCanvasCtx.fillStyle = cssColor
  colorCanvasCtx.fillRect(0, 0, 1, 1)
  const [r, g, b, a] = colorCanvasCtx.getImageData(0, 0, 1, 1).data
  if (a === 0) return fallback
  return `rgb(${r}, ${g}, ${b})`
}

interface Palette {
  satActive: string
  inactive: string
  islLink: string
  groundLink: string
  routeAccent: string
  client: string
  gateway: string
  error: string
}

function buildPalette(): Palette {
  return {
    satActive: resolveThemeColor('--ui-info', 'rgb(56, 189, 248)'),
    inactive: resolveThemeColor('--ui-text-dimmed', 'rgb(107, 114, 128)'),
    islLink: resolveThemeColor('--ui-border-accented', 'rgb(71, 85, 105)'),
    groundLink: resolveThemeColor('--ui-primary', 'rgb(14, 165, 233)'),
    routeAccent: resolveThemeColor('--ui-warning', 'rgb(245, 158, 11)'),
    client: resolveThemeColor('--ui-success', 'rgb(34, 197, 94)'),
    gateway: resolveThemeColor('--ui-secondary', 'rgb(168, 85, 247)'),
    error: resolveThemeColor('--ui-error', 'rgb(239, 68, 68)')
  }
}

/** Добавляет альфу к уже разрешённому `rgb(r, g, b)` — для «приглушённых» базовых линий. */
function withAlpha(rgb: string, alpha: number): string {
  const m = /^rgba?\(([^)]+)\)$/.exec(rgb)
  if (!m || !m[1]) return rgb
  const [r, g, b] = m[1].split(',').map((part) => part.trim())
  return `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

const palette = ref<Palette>(buildPalette())
const colorMode = useColorMode()
watch(() => colorMode.value, () => {
  nextTick(() => {
    palette.value = buildPalette()
    refreshLayers()
  })
})

// ────────────────────────────────── радиус «зоны видимости» ──────────────────────────────────
/**
 * Угловой радиус (в градусах, ровно единица `ringMaxRadius` у globe.gl) окружности на поверхности
 * Земли, внутри которой спутник виден под углом ≥ `min_elevation_deg`. Обычная теорема синусов
 * в треугольнике «центр Земли — спутник — точка на поверхности»: угол в точке P равен 90° + ε
 * (элевация отсчитывается от местного горизонта, до надира — 90° + ε), значит
 * sin(S) = (R / r)·cos(ε), а центральный угол λ = 90° − ε − S. Статическая функция двух скаляров
 * сценария — не имеет отношения к пропагации орбиты (`RAAN`/`phase`/вращение Земли), поэтому не
 * повторяет `geometry.py`; `R` — тот же радиус Земли, что и там, взят как константа отрисовки.
 */
function footprintRadiusDeg(altitudeKm: number, minElevationDeg: number): number {
  const R = 6371
  const r = R + altitudeKm
  const elevRad = (minElevationDeg * Math.PI) / 180
  const sinS = Math.min(1, Math.max(-1, (R / r) * Math.cos(elevRad)))
  const lambdaRad = Math.PI / 2 - elevRad - Math.asin(sinS)
  return Math.max(0, (lambdaRad * 180) / Math.PI)
}

// ────────────────────────────────── производные данные ──────────────────────────────────

const hasData = computed(() => props.satellites.length > 0 || props.groundSites.length > 0)

/** Единое пространство координат узлов снимка: спутники по lat/lon/alt_ratio, земля — по alt 0. */
const nodeById = computed(() => {
  const m = new Map<string, { lat: number, lng: number, alt: number }>()
  for (const sat of props.satellites) m.set(sat.id, { lat: sat.lat_deg, lng: sat.lon_deg, alt: sat.alt_ratio })
  for (const site of props.groundSites) m.set(site.id, { lat: site.lat_deg, lng: site.lon_deg, alt: 0 })
  return m
})
function coordOf(id: string): { lat: number, lng: number, alt: number } {
  return nodeById.value.get(id) ?? { lat: 0, lng: 0, alt: 0 }
}

const routeByClient = computed(() => {
  const m = new Map<string, GlobeRoute>()
  for (const r of props.routes) m.set(r.client_id, r)
  return m
})
const selectedRoute = computed(() => (props.selectedClientId ? routeByClient.value.get(props.selectedClientId) ?? null : null))
/** Узлы выбранного маршрута — для подсветки точек/подписей, которые он проходит. */
const routeNodeIds = computed(() => new Set(selectedRoute.value?.path ?? []))

function reasonLabel(reason: GlobeRouteReason | null | undefined): string {
  return reason ? REASON_LABELS[reason] : 'Причина не определена.'
}

// ── точки: аппараты ──
function satColor(d: GlobeSatellite): string {
  if (!d.active) return palette.value.inactive
  if (routeNodeIds.value.has(d.id)) return palette.value.routeAccent
  return palette.value.satActive
}
function satRadius(d: GlobeSatellite): number {
  if (routeNodeIds.value.has(d.id)) return 0.42
  return d.active ? 0.26 : 0.2
}
function satelliteLabel(d: GlobeSatellite): string {
  const status = d.active ? 'активен' : 'отказал'
  const onRoute = routeNodeIds.value.has(d.id) ? ' · на выбранном маршруте' : ''
  return `<div style="font:12px/1.4 ui-sans-serif,system-ui;padding:4px 8px;border-radius:6px;`
    + `background:var(--ui-bg-elevated);color:var(--ui-text-highlighted);border:1px solid var(--ui-border);">`
    + `<b>${escapeHtml(d.id)}</b><br/>${status}${onRoute}</div>`
}

// ── дуги: ISL, наземные контакты и подсвеченный маршрут одним слоем (см. докстринг выше) ──
type ArcKind = 'isl' | 'ground' | 'route'
interface ArcDatum {
  kind: ArcKind
  aLat: number
  aLng: number
  aAlt: number
  bLat: number
  bLng: number
  bAlt: number
  info: string
}
const mergedArcs = computed<ArcDatum[]>(() => {
  const out: ArcDatum[] = []
  for (const e of props.islEdges) {
    const a = coordOf(e.a)
    const b = coordOf(e.b)
    out.push({
      kind: 'isl', aLat: a.lat, aLng: a.lng, aAlt: a.alt, bLat: b.lat, bLng: b.lng, bAlt: b.alt,
      info: `ISL ${escapeHtml(e.a)} – ${escapeHtml(e.b)}${e.dist_km != null ? `: ${Math.round(e.dist_km)} км` : ''}`
    })
  }
  for (const c of props.groundContacts) {
    const a = coordOf(c.ground_id)
    const b = coordOf(c.satellite_id)
    const elev = c.elevation_deg != null ? `${c.elevation_deg.toFixed(1)}°` : '—'
    out.push({
      kind: 'ground', aLat: a.lat, aLng: a.lng, aAlt: a.alt, bLat: b.lat, bLng: b.lng, bAlt: b.alt,
      info: `${escapeHtml(c.ground_id)} → ${escapeHtml(c.satellite_id)}${c.dist_km != null ? `: ${Math.round(c.dist_km)} км` : ''}${c.elevation_deg != null ? `, угол ${elev}` : ''}`
    })
  }
  const route = selectedRoute.value
  if (route && route.path.length >= 2) {
    for (let i = 0; i < route.path.length - 1; i++) {
      const fromId = route.path[i]!
      const toId = route.path[i + 1]!
      const a = coordOf(fromId)
      const b = coordOf(toId)
      out.push({
        kind: 'route', aLat: a.lat, aLng: a.lng, aAlt: a.alt, bLat: b.lat, bLng: b.lng, bAlt: b.alt,
        info: `Маршрут ${escapeHtml(route.client_id)}: ${escapeHtml(fromId)} → ${escapeHtml(toId)}`
      })
    }
  }
  return out
})
function arcColorFor(d: ArcDatum): string {
  if (d.kind === 'route') return palette.value.routeAccent
  if (d.kind === 'isl') return withAlpha(palette.value.islLink, 0.55)
  return withAlpha(palette.value.groundLink, 0.45)
}

// ── география: страны и города-миллионники ──
// Границы — Natural Earth admin_0 (110m) в варианте по умолчанию: он отражает ФАКТИЧЕСКИЙ
// контроль территории, а не признание. Проверено по данным: Крым отнесён к России, Западная
// Сахара к Марокко, Косово и Северный Кипр — отдельные объекты. Оговорка: Абхазия и Южная
// Осетия в этом наборе остаются внутри Грузии.
// Города — те же данные, фильтр POP_MAX ≥ 1 млн, 395 точек. Оба файла лежат локально
// в public/geo: сервис обязан работать без интернета.
interface CityDatum { kind: 'city', id: string, name: string, lat_deg: number, lon_deg: number, pop: number }
type LabelDatum = (GlobeGroundSite & { kind?: 'site' }) | CityDatum

const countries = ref<object[]>([])
const cities = ref<CityDatum[]>([])

async function loadGeography(): Promise<void> {
  try {
    const [c, p] = await Promise.all([
      fetch('/geo/countries.json').then(r => r.json()),
      fetch('/geo/cities.json').then(r => r.json())
    ])
    countries.value = c.features ?? []
    cities.value = (p as [string, number, number, number][]).map(([name, lon, lat, pop]) => ({
      kind: 'city' as const, id: `city:${name}`, name, lat_deg: lat, lon_deg: lon, pop
    }))
  } catch {
    // География — оформление, а не данные расчёта: её отсутствие не должно ронять глобус.
    countries.value = []
    cities.value = []
  }
}

const isCity = (d: LabelDatum): d is CityDatum => (d as CityDatum).kind === 'city'
/** Подпись только у крупнейших: 395 названий на шаре превращаются в кашу и перекрывают сеть. */
const CITY_LABEL_FROM = 5_000_000
/** Города с подписями — отдельный DOM-слой globe.gl (`htmlElementsData`): обычные элементы
 *  страницы, поэтому кириллица, шрифт и цвет темы работают как везде. */
const namedCities = computed(() => cities.value.filter(c => c.pop >= CITY_LABEL_FROM))

const mergedLabels = computed<LabelDatum[]>(() => [
  ...cities.value,
  ...props.groundSites.map(s => ({ ...s, kind: 'site' as const }))
])

// ── подписи: наземные пункты и шлюз ──
function groundLabelText(d: GlobeGroundSite): string {
  return d.id
}
function labelColorFor(d: LabelDatum): string {
  if (isCity(d)) return withAlpha(palette.value.inactive, d.pop >= CITY_LABEL_FROM ? 0.75 : 0.45)
  if (d.role === 'gateway') return palette.value.gateway
  const route = routeByClient.value.get(d.id)
  if (!route || route.path.length === 0) return palette.value.error
  if (d.id === props.selectedClientId) return palette.value.routeAccent
  return palette.value.client
}
function labelDotRadiusFor(d: LabelDatum): number {
  // Радиус точки города растёт с населением, но остаётся заметно меньше наземного пункта:
  // география — это контекст, а не предмет расчёта.
  if (isCity(d)) return Math.min(0.22, 0.07 + Math.log10(d.pop / 1e6 + 1) * 0.12)
  return d.role === 'gateway' || d.id === props.selectedClientId ? 0.5 : 0.32
}

// ── кольца: зона видимости у спутников, которые сейчас кого-то обслуживают ──
interface RingDatum { lat: number, lng: number, radiusDeg: number, accent: boolean }
const footprintRadius = computed<number | null>(() => (
  props.altitudeKm != null && props.minElevationDeg != null
    ? footprintRadiusDeg(props.altitudeKm, props.minElevationDeg)
    : null
))
const servingSatelliteIds = computed(() => new Set(props.groundContacts.map((c) => c.satellite_id)))
const footprintRings = computed<RingDatum[]>(() => {
  const radius = footprintRadius.value
  if (radius == null) return []
  return props.satellites
    .filter((s) => s.active && servingSatelliteIds.value.has(s.id))
    .map((s) => ({ lat: s.lat_deg, lng: s.lon_deg, radiusDeg: radius, accent: routeNodeIds.value.has(s.id) }))
})

// ── наведение: короткая сводка снизу глобуса (UBadge в шаблоне) ──
const hoverInfo = ref<string | null>(null)
function describeSatellite(d: GlobeSatellite): string {
  return `${d.id} · ${d.active ? 'активен' : 'отказал'} · alt ${d.alt_ratio.toFixed(3)} R⊕`
}
function describeGround(d: GlobeGroundSite): string {
  return `${d.name} (${d.id}) · ${d.role === 'gateway' ? 'шлюз' : 'клиентский пункт'}`
}

// ────────────────────────────────── жизненный цикл globe.gl ──────────────────────────────────
// .claude/rules/frontend.md: инстанс создаётся в onMounted по ref контейнера, в onBeforeUnmount —
// pauseAnimation() и очистка контейнера, иначе WebGL-контексты копятся на hot-reload/повторном
// монтировании и вкладка умирает к середине работы.
const rootRef = ref<HTMLDivElement | null>(null)
const containerRef = ref<HTMLDivElement | null>(null)
let globe: GlobeInstance | null = null
let resizeObserver: ResizeObserver | null = null

function refreshLayers(): void {
  if (!globe) return
  globe.pointsData(props.satellites as unknown as object[])
  globe.arcsData(mergedArcs.value as unknown as object[])
  globe.labelsData(mergedLabels.value as unknown as object[])
  globe.polygonsData(countries.value)
  globe.htmlElementsData(namedCities.value as unknown as object[])
  globe.ringsData(footprintRings.value as unknown as object[])
  globe.pathsData(props.trails as unknown as object[])
}

function syncSize(): void {
  if (!globe || !rootRef.value) return
  const rect = rootRef.value.getBoundingClientRect()
  globe.width(Math.max(1, Math.round(rect.width))).height(Math.max(1, Math.round(rect.height)))
}

function resetView(): void {
  globe?.pointOfView({ lat: 25, lng: 40, altitude: 2.3 }, 600)
}

onMounted(() => {
  if (!containerRef.value) return
  palette.value = buildPalette()

  // География подгружается после создания сцены и обновляет слои по готовности: ждать её
  // до первого кадра незачем — группировка важнее оформления.
  void loadGeography().then(() => refreshLayers())

  globe = new Globe(containerRef.value, { rendererConfig: { antialias: true, alpha: true } })
    .globeImageUrl(EARTH_TEXTURE)
    .bumpImageUrl(BUMP_TEXTURE)
    .backgroundImageUrl(STARFIELD_TEXTURE)
    .showAtmosphere(true)
    .atmosphereColor(palette.value.satActive)
    .atmosphereAltitude(0.2)
    // аппараты
    .pointLat('lat_deg')
    .pointLng('lon_deg')
    .pointAltitude((d) => (d as GlobeSatellite).alt_ratio)
    .pointColor((d) => satColor(d as GlobeSatellite))
    .pointRadius((d) => satRadius(d as GlobeSatellite))
    .pointLabel((d) => satelliteLabel(d as GlobeSatellite))
    .pointsTransitionDuration(0)
    .onPointHover((p) => { hoverInfo.value = p ? describeSatellite(p as GlobeSatellite) : null })
    // ISL + наземные линии + выбранный маршрут (arcDashAnimateTime — только у маршрута)
    .arcStartLat((d) => (d as ArcDatum).aLat)
    .arcStartLng((d) => (d as ArcDatum).aLng)
    .arcEndLat((d) => (d as ArcDatum).bLat)
    .arcEndLng((d) => (d as ArcDatum).bLng)
    .arcStartAltitude((d) => (d as ArcDatum).aAlt)
    .arcEndAltitude((d) => (d as ArcDatum).bAlt)
    // `null` отдал бы дугу на откуп arcAltitudeAutoScale: вершина уезжает на половину расстояния
    // между концами, и связь длиной 2700 км выгибается на ~1350 км в космос. Физически ISL —
    // почти прямая хорда, поэтому вершину держим на высоте самих аппаратов. Маршрут намеренно
    // приподнят над остальными связями, чтобы читаться поверх них.
    .arcAltitude((d) => {
      const a = d as ArcDatum
      return a.kind === 'route' ? 0.16 : Math.max(a.aAlt, a.bAlt)
    })
    .arcColor((d: object) => arcColorFor(d as ArcDatum))
    .arcStroke((d) => ((d as ArcDatum).kind === 'route' ? 0.6 : 0.22))
    .arcDashLength((d) => ((d as ArcDatum).kind === 'route' ? 0.4 : 1))
    .arcDashGap((d) => ((d as ArcDatum).kind === 'route' ? 0.25 : 0))
    .arcDashAnimateTime((d) => ((d as ArcDatum).kind === 'route' ? 1600 : 0))
    .arcLabel((d) => `<div style="font:12px ui-sans-serif,system-ui;padding:4px 8px;border-radius:6px;`
      + `background:var(--ui-bg-elevated);color:var(--ui-text-highlighted);border:1px solid var(--ui-border);">`
      + `${(d as ArcDatum).info}</div>`)
    .arcsTransitionDuration(0)
    .onArcHover((a) => { hoverInfo.value = a ? (a as ArcDatum).info : null })
    // наземные пункты и шлюз
    .labelLat('lat_deg')
    .labelLng('lon_deg')
    // Города здесь только точкой, без текста: подписи globe.gl рисуются геометрией по
    // латинскому typeface (three-globe), и кириллица в них превращается в «?». Названия
    // городов выводятся DOM-слоем ниже — там работает обычный шрифт страницы.
    .labelText((d) => (isCity(d as LabelDatum) ? '' : groundLabelText(d as LabelDatum)))
    .labelColor((d) => labelColorFor(d as LabelDatum))
    .labelDotRadius((d) => labelDotRadiusFor(d as LabelDatum))
    .labelIncludeDot(true)
    .labelSize((d) => (isCity(d as LabelDatum) ? 0.55 : 1.1))
    .labelAltitude(0.006)
    .labelResolution(3)
    .labelsTransitionDuration(0)
    .onLabelHover((l) => {
      if (!l) { hoverInfo.value = null; return }
      const d = l as LabelDatum
      hoverInfo.value = isCity(d)
        ? `${escapeHtml(d.name)} — ${(d.pop / 1e6).toFixed(1)} млн`
        : describeGround(d)
    })
    .onLabelClick((l) => {
      const d = l as LabelDatum
      if (!isCity(d) && d.role === 'client') emit('select-client', d.id)
    })
    // Страны: только контур. Глобус остаётся видом сети, а не атласом — границы дают
    // географическую привязку и не спорят с группировкой за внимание.
    .polygonCapColor(() => 'rgba(0, 0, 0, 0)')
    .polygonSideColor(() => 'rgba(0, 0, 0, 0)')
    .polygonStrokeColor(() => withAlpha(palette.value.satActive, 0.35))
    .polygonAltitude(0.004)
    .polygonLabel((d) => {
      const n = (d as { properties?: { n?: string } }).properties?.n
      return n ? `<div style="font-size:12px">${escapeHtml(n)}</div>` : ''
    })
    .polygonsTransitionDuration(0)
    // Названия крупнейших городов обычными DOM-элементами — единственный способ показать
    // кириллицу на глобусе.
    .htmlLat('lat_deg')
    .htmlLng('lon_deg')
    .htmlAltitude(0.012)
    .htmlElement((d) => {
      const c = d as CityDatum
      const el = document.createElement('div')
      el.textContent = c.name
      el.title = `${c.name} — ${(c.pop / 1e6).toFixed(1)} млн`
      el.style.cssText = [
        'font-size:10px',
        'line-height:1',
        'white-space:nowrap',
        'pointer-events:none',
        'transform:translate(6px, -50%)',
        'text-shadow:0 0 4px rgba(0,0,0,.9)',
        `color:${withAlpha(palette.value.inactive, 0.9)}`
      ].join(';')
      return el
    })
    .htmlTransitionDuration(0)
    // Следы: градиент от прозрачного к цвету аппарата — хвост тает, голова яркая.
    .pathPoints((d) => (d as { points: [number, number, number][] }).points)
    .pathPointLat((pt) => (pt as [number, number, number])[0])
    .pathPointLng((pt) => (pt as [number, number, number])[1])
    .pathPointAlt((pt) => (pt as [number, number, number])[2])
    .pathColor(() => ['rgba(0,0,0,0)', withAlpha(palette.value.satActive, 0.55)])
    .pathStroke(0.4)
    .pathTransitionDuration(0)
    // зона видимости обслуживающих спутников
    .ringLat((d) => (d as RingDatum).lat)
    .ringLng((d) => (d as RingDatum).lng)
    .ringAltitude(0.002)
    .ringMaxRadius((d) => (d as RingDatum).radiusDeg)
    .ringPropagationSpeed(4)
    .ringRepeatPeriod(1800)
    .ringColor((d: object) => {
      const ring = d as RingDatum
      const base = ring.accent ? palette.value.routeAccent : palette.value.satActive
      return (t: number) => withAlpha(base, Math.max(0, 1 - t))
    })
    .enablePointerInteraction(true)

  globe.pointOfView({ lat: 25, lng: 40, altitude: 2.3 })
  refreshLayers()
  syncSize()

  resizeObserver = new ResizeObserver(syncSize)
  resizeObserver.observe(rootRef.value as HTMLDivElement)
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (globe) {
    globe.pauseAnimation()
    globe._destructor()
  }
  if (containerRef.value) containerRef.value.innerHTML = ''
  globe = null
  if (colorProbeEl) {
    colorProbeEl.remove()
    colorProbeEl = null
  }
  colorCanvasCtx = null
})

watch(
  [
    () => props.satellites, () => props.islEdges, () => props.groundContacts,
    () => props.groundSites, () => props.routes, () => props.selectedClientId,
    () => props.altitudeKm, () => props.minElevationDeg
  ],
  () => refreshLayers()
)
</script>

<template>
  <div ref="rootRef" class="relative w-full h-full min-h-0 overflow-hidden bg-default">
    <div ref="containerRef" class="absolute inset-0" />

    <div
      v-if="loading"
      class="absolute inset-0 flex items-center justify-center gap-3 flex-col bg-default/85 backdrop-blur-sm"
    >
      <USkeleton class="size-40 rounded-full" />
      <span class="text-sm text-muted">Расчёт снимка сети…</span>
    </div>

    <div v-else-if="!hasData" class="absolute inset-0 flex items-center justify-center pointer-events-none">
      <UEmpty
        icon="i-lucide-globe"
        title="Нет данных снимка"
        description="Загрузите сценарий и запросите снимок сети — здесь появится группировка."
      />
    </div>

    <template v-else>
      <div class="absolute top-3 left-3 right-3 flex items-start justify-between gap-2 pointer-events-none">
        <div class="flex flex-wrap gap-2 pointer-events-auto">
          <UBadge
            :label="`Активны: ${satellites.filter((s) => s.active).length}/${satellites.length}`"
            color="primary"
            variant="subtle"
            icon="i-lucide-satellite"
          />
          <UBadge
            v-if="islEdges.length"
            :label="`ISL: ${islEdges.length}`"
            color="neutral"
            variant="subtle"
            icon="i-lucide-git-commit-horizontal"
          />
          <UBadge
            v-if="groundContacts.length"
            :label="`Наземных контактов: ${groundContacts.length}`"
            color="neutral"
            variant="subtle"
            icon="i-lucide-antenna"
          />
        </div>

        <div class="flex gap-2 pointer-events-auto">
          <UPopover>
            <UButton icon="i-lucide-info" color="neutral" variant="subtle" square size="sm" aria-label="Легенда" />
            <template #content>
              <div class="p-3 flex flex-col gap-1.5 text-xs w-60">
                <div class="flex items-center gap-2">
                  <span class="size-2.5 rounded-full bg-info shrink-0" /> Активный аппарат
                </div>
                <div class="flex items-center gap-2">
                  <span class="size-2.5 rounded-full shrink-0" style="background-color: var(--ui-text-dimmed)" /> Отказавший аппарат (на орбите, вне связей)
                </div>
                <div class="flex items-center gap-2">
                  <span class="size-2.5 rounded-full bg-warning shrink-0" /> Узел/дуга выбранного маршрута
                </div>
                <div class="flex items-center gap-2">
                  <span class="size-2.5 rounded-full bg-success shrink-0" /> Клиентский пункт с маршрутом
                </div>
                <div class="flex items-center gap-2">
                  <span class="size-2.5 rounded-full bg-error shrink-0" /> Клиентский пункт без маршрута сейчас
                </div>
                <div class="flex items-center gap-2">
                  <span class="size-2.5 rounded-full bg-secondary shrink-0" /> Шлюз
                </div>
                <p class="text-muted pt-1 border-t border-default mt-1">
                  Кольцо вокруг спутника — зона видимости (≥ min_elevation_deg) у аппаратов,
                  которые сейчас кого-то обслуживают.
                </p>
              </div>
            </template>
          </UPopover>

          <UTooltip text="Сбросить вид">
            <UButton
              icon="i-lucide-locate-fixed"
              color="neutral"
              variant="subtle"
              square
              size="sm"
              aria-label="Сбросить вид"
              @click="resetView"
            />
          </UTooltip>
        </div>
      </div>

      <div class="absolute bottom-3 left-3 right-3 flex items-end justify-between gap-2 pointer-events-none">
        <UBadge
          v-if="hoverInfo"
          :label="hoverInfo"
          color="neutral"
          variant="soft"
          class="pointer-events-auto max-w-[60%] truncate"
        />
        <div v-else />

        <UEmpty
          v-if="selectedClientId && selectedRoute && selectedRoute.path.length === 0"
          class="pointer-events-auto max-w-xs"
          size="sm"
          icon="i-lucide-unlink"
          title="Маршрута нет"
          :description="reasonLabel(selectedRoute.reason)"
        />
        <UBadge
          v-else-if="!selectedClientId"
          label="Кликните по клиентскому пункту, чтобы выделить его маршрут"
          color="neutral"
          variant="soft"
          class="pointer-events-auto"
        />
      </div>
    </template>
  </div>
</template>
