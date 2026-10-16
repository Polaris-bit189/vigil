/**
 * 主题的**状态源**：id 列表、读取、落盘、应用。
 *
 * 三层分工（M7 全局约定）：
 *   ① 原始变量 `--v-*` 定义在 `index.css` 的 `:root` / `[data-theme='x']` 里；
 *   ② `@theme inline` 把它们接进 Tailwind 命名空间（调用点的类名固定，值随主题走）；
 *   ③ 这个文件只管「读 localStorage → 挂 `data-theme` → 同步 `<meta theme-color>`」。
 *
 * ⚠️ `theme-color` 的值**从 CSS 现读**，不在 TS 里再抄一份色表：抄一份就是两处真相，
 * 而 `index.html` 的防闪脚本已经证明这种重复必然漂移（那条白名单有测试钉着）。
 */

import { applyArgusColor, clearArgusColor, readArgusColor } from './argusColor.ts'

export type ThemeId = 'default' | 'material' | 'brutalism' | 'vaporwave' | 'glass' | 'argus'

export const THEMES: { id: ThemeId; label: string }[] = [
  { id: 'default', label: '默认' },
  { id: 'material', label: 'Material Design' },
  { id: 'brutalism', label: '粗野主义' },
  { id: 'vaporwave', label: '蒸汽波' },
  { id: 'glass', label: '毛玻璃' },
  { id: 'argus', label: 'Project ARGUS' },   // ⚠️ 它在菜单里由 `ArgusLabel` 渲染成"先乱码后显名"（Task 3）
]

/**
 * 主题生效的**唯一入口**（`applyTheme` 内部调它）：ARGUS 要带上它的颜色覆盖，别的主题要清干净。
 *
 * ⚠️ **为什么"切走时要清"不是可选项**：ARGUS 的颜色写在 `:root` 的**行内**自定义属性上，
 * 而行内赢过**任何**选择器（包括 `[data-theme='material']`）⇒ 不清的话，材料的字色会被
 * 上一次 ARGUS 的颜色残留顶掉（`data-theme` 明明是 material，屏幕上却是荧光绿）。
 */
function syncArgusColor(id: ThemeId): void {
  if (id === 'argus') applyArgusColor(readArgusColor())
  else clearArgusColor()
}

export const THEME_STORAGE_KEY = 'vigil.theme'

export function isThemeId(v: unknown): v is ThemeId {
  return typeof v === 'string' && THEMES.some((t) => t.id === v)
}

/** localStorage 可能整个不可用（无痕/被策略挡）——读写都要能静默失败。 */
function stored(): unknown {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY)
  } catch {
    return null
  }
}

export function readTheme(): ThemeId {
  const v = stored()
  return isThemeId(v) ? v : 'default'
}

/** 挂属性 + 落盘 + 同步 `theme-color`。出口判据 2「刷新后仍在」靠它。 */
export function applyTheme(id: ThemeId): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  root.setAttribute('data-theme', id)
  // ⚠️ 照计划放在 `setAttribute` **之后**（顺序本身不是判据：行内变量赢过选择器是 CSS
  // 层的事，与这两行的先后无关）。**真正 load-bearing 的是"每次 `applyTheme` 都必须调它"**
  // ——ARGUS 的颜色是 `:root` 行内变量，没有这一步，切到 material 时上一条 ARGUS 的颜色
  // 会继续压着材料的字色（`data-theme` 已是 material，屏幕上却是荧光绿）。
  syncArgusColor(id)
  try {
    localStorage.setItem(THEME_STORAGE_KEY, id)
  } catch {
    // 存不下只影响「下次还记得」这一件事，不该让当前这次切换失败
  }
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta !== null) {
    // 属性刚改完，getComputedStyle 会强制样式重算 ⇒ 拿到的就是新主题的 --v-bg
    const bg = getComputedStyle(root).getPropertyValue('--v-bg').trim()
    if (bg !== '') meta.setAttribute('content', bg)
  }
}
