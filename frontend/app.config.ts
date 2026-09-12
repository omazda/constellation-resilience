// Тема Nuxt UI — только токены цвета, без форка компонентов (.claude/rules/frontend.md).
// `sky` вместо дефолтного `green`: тема телеметрии/космоса, достаточно контраста для статусов
// связи (активен/потерян/отказал) на карте и в бейджах.
export default defineAppConfig({
  ui: {
    colors: {
      primary: 'sky',
      neutral: 'slate'
    }
  }
})
