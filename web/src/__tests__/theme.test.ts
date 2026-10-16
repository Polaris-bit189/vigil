import { afterEach, expect, test, vi } from 'vitest'
import { THEMES, THEME_STORAGE_KEY, applyTheme, isThemeId, readTheme } from '../theme.ts'

// ⚠️ 本仓**没有 `@types/node`**（`web/package.json` 不在 T1 的 Interfaces 里，
// 不许为了这个去加依赖），而 `tsc -b`（在 `npm run build` 里）会把 `src/**`
// 连测试一起编译 ⇒ 写成 `import { readFileSync } from 'node:fs'` 会以
// `TS2307: Cannot find module 'node:fs'` 让**构建失败**（实测，2026-09-22）。
// 用**具名变量**当模块说明符可以挡掉 TS 的静态解析（它解析不了 ⇒ 不报错），
// 类型由 `as` 显式写死（不是 `any`）；运行期 vitest 照常解析 `node:fs`（实测通过）。
// ⛔ 不用 `// @ts-expect-error`：将来真装了 `@types/node`，那条指令会变成
// TS2578「未使用的 expect-error」——等于把构建又搞坏一次。
const NODE_FS = 'node:' + 'fs'
const { readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as {
  readFileSync: (p: URL, enc: string) => string
}

// ⚠️ 必须先把 `import.meta.url` 收进一个变量再用（实测，2026-09-22）：
// `new URL('…', import.meta.url)` 这种**第二个参数是字面量**的写法会被 Vite 的
// asset-import-meta-url 变换接管，解析基准变成 jsdom 的 document base
// （`http://localhost:3000/index.html`），而 `readFileSync` 只认 `file:`
// ⇒ 报 `ERR_INVALID_URL_SCHEME: The URL must be of scheme file`。
// 实测对照（同一文件、同一行、只差一个间接层）：
//   `new URL('../../index.html', import.meta.url)` → http://localhost:3000/index.html
//   `const HERE = import.meta.url; new URL('../../index.html', HERE)` → file:///<仓库根>/web/index.html
const HERE = import.meta.url

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.documentElement.removeAttribute('style')
})

test('没存过 ⇒ 默认主题', () => {
  expect(readTheme()).toBe('default')
})

test('存过合法值 ⇒ 读回它', () => {
  localStorage.setItem(THEME_STORAGE_KEY, 'vaporwave')
  expect(readTheme()).toBe('vaporwave')
})

test('⚠️ 存了非法值 ⇒ 回落默认，不许把垃圾挂到 DOM 上', () => {
  // 旧版本遗留 / 手改 / 别的应用写串了键——这些都不是"异常"，是**输入**。
  // 不校验的话 `data-theme` 会挂上垃圾值，而 CSS 侧静默回落到 :root
  //（看起来"正常"），排查时毫无线索。
  localStorage.setItem(THEME_STORAGE_KEY, '{"theme":"dark"}')
  expect(readTheme()).toBe('default')
})

test('isThemeId 认六套、拒其余', () => {
  // ⭐ T8-1：5 → **6**（Project ARGUS）。这是**设计变更**，不是"把断言放宽到能过"——
  // 数字本身没变松，多出来的那套在 `index.css` 有块、在 `index.html` 白名单里有名
  //（下一条用例逐字比对两者）。
  expect(THEMES).toHaveLength(6)
  for (const t of THEMES) expect(isThemeId(t.id)).toBe(true)
  expect(isThemeId('dark')).toBe(false)
  expect(isThemeId(null)).toBe(false)
})

test('applyTheme：挂属性 + 落盘 + 同步 <meta name="theme-color">', () => {
  const meta = document.createElement('meta')
  meta.setAttribute('name', 'theme-color')
  document.head.appendChild(meta)
  // ⚠️ theme-color 的值**从 CSS 现读**（`getComputedStyle` 取 `--v-bg`），
  // 不在 TS 里抄一份色表——抄一份就是两处真相，必然漂移。
  // jsdom 不加载 index.css，所以这里用**内联**自定义属性模拟"CSS 已经给了值"。
  // 计划期空针实测：jsdom 的 getComputedStyle 能读到内联自定义属性（断言 `toBe('#123456')` 通过）。
  document.documentElement.style.setProperty('--v-bg', '#123456')

  applyTheme('glass')

  expect(document.documentElement.getAttribute('data-theme')).toBe('glass')
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('glass')
  expect(meta.getAttribute('content')).toBe('#123456')
  meta.remove()
})

test('localStorage 抛错时（无痕/被策略挡）不许把切换本身搞崩', () => {
  const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new DOMException('denied')
  })
  expect(readTheme()).toBe('default')
  spy.mockRestore()

  const spy2 = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('denied')
  })
  expect(() => applyTheme('material')).not.toThrow()
  expect(document.documentElement.getAttribute('data-theme')).toBe('material')
  spy2.mockRestore()
})

test('index.html 的防闪白名单不许与 THEMES 漂移', () => {
  // 白名单是**手写的第二份真相**（第一份是本文件的 THEMES）。两份必然漂移，
  // 所以钉一条机械比对——它红的时候，改哪一份都对，但不能只有一份对。
  const html = readFileSync(new URL('../../index.html', HERE), 'utf8')
  const m = html.match(/var ALLOWED = \[([^\]]*)\]/)
  expect(m, 'index.html 里找不到 ALLOWED 白名单').toBeTruthy()
  const ids = m![1]
    .split(',')
    .map((s) => s.trim().replace(/^'|'$/g, ''))
    .filter(Boolean)
  expect(ids).toEqual(THEMES.map((t) => t.id))
})
