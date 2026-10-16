import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import App from '../App.tsx'
import ThemePicker from '../components/ThemePicker.tsx'

// ⚠️ 最后那条用例（走真实 `App` 接线的）会真的给 `<html>` 挂上 `data-theme`，
// 并在盘上留下 `vigil.theme` ——**这条负责清**（在这条用例出现之前，
// 它一直没有对象可清）。`theme.test.ts:27` 的 afterEach 清的是同一对；
// `vi.unstubAllGlobals` 撤掉那条用例打的 fetch 桩。
afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
  localStorage.removeItem('vigil.theme')
  vi.unstubAllGlobals()
})

/**
 * `App` 会渲染 `Feed` ⇒ 一挂载就发请求。桩法与 `App.dom.test.tsx:8` 的
 * `installFetch` **同一套**（categories / persons / items / undo 各回一个空壳）。
 * 这里不记调用流水（这条用例只关心主题的两个落点），所以也不看 `init?.method`。
 */
function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/undo')) return ok({ undone: false })
      if (url.startsWith('/api/categories')) return ok({ categories: [] })
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      if (url.startsWith('/api/items')) return ok({ items: [], total: 0, limit: 50, offset: 0 })
      return ok({})
    }),
  )
}

test('默认关闭；点 🎨 展开六套，当前那套被标为选中', async () => {
  render(<ThemePicker value="material" onChange={() => {}} />)
  expect(screen.queryByRole('menu')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))
  // ⭐ T8-1：5 → **6**（Project ARGUS）。与 `theme.test.ts` 那条同性质：**设计变更**，
  // 不是"把断言放宽到能过"——菜单项直接来自 `THEMES`，多一套就多一项，这条数字就是
  // "菜单渲染了几项"的唯一判据（漏渲染某一套时它必红）。
  expect(screen.getAllByRole('menuitemradio')).toHaveLength(6)
  // ⚠️ 本仓**没有** `@testing-library/jest-dom`（devDependencies 里没有、test-setup 也没引）
  // ⇒ `toHaveAttribute` 会以「Invalid Chai property」失败——那条失败与被测行为无关。
  // 按本仓既有写法（theme.test.ts:69/71/86）用 `getAttribute(...) + toBe(...)`，
  // 断言的**语义一字不改**（aria-checked 恰好是 'true' / 'false'）。
  expect(
    screen.getByRole('menuitemradio', { name: 'Material Design' }).getAttribute('aria-checked'),
  ).toBe('true')
  expect(
    screen.getByRole('menuitemradio', { name: '毛玻璃' }).getAttribute('aria-checked'),
  ).toBe('false')
})

test('选一套 ⇒ 回调收到它、菜单收起', async () => {
  const onChange = vi.fn()
  render(<ThemePicker value="default" onChange={onChange} />)
  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))
  await userEvent.click(screen.getByRole('menuitemradio', { name: '蒸汽波' }))
  expect(onChange).toHaveBeenCalledWith('vaporwave')
  expect(screen.queryByRole('menu')).toBeNull()
})

test('点外面 / 按 Esc 都能关（没有这两条，手机上关不掉）', async () => {
  render(<ThemePicker value="default" onChange={() => {}} />)
  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))
  await userEvent.click(document.body)
  expect(screen.queryByRole('menu')).toBeNull()

  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('menu')).toBeNull()
})

/**
 * ⭐ 这条守的是**里程碑判据 2**：「切换后刷新仍在」。
 *
 * ⚠️ 为什么必须走**真实 `App` 接线**：上面三条都只验 `onChange` 回调
 * （`render(<ThemePicker onChange={...} />)`），于是把 `App.tsx` 里的
 * `applyTheme(id)` 整个删掉、或把 `onChange` 接成 `() => {}`，
 * 那三条**照样全绿**——"刷新还在不在"没有任何一条在验。
 * 落盘与挂属性都发生在 App 的接线里（`pickTheme`），所以守卫得从 App 的门口进去。
 *
 * ⚠️ 变异反证：把 `App.tsx` 的 `pickTheme` 改成只有 `setTheme(id)`
 * （去掉 `applyTheme(id)`）⇒ 本条**必红**（两个断言各红一条）。
 *
 * ⚠️ 这里断言的是**字面量** `'vigil.theme'`，不是 `THEME_STORAGE_KEY`：
 * 判据 2 的另一半是 `index.html:25` 那段防闪脚本里的
 * `localStorage.getItem('vigil.theme')`——它读的就是这个字面量。
 * 用常量的话，日后改一次键名本条仍然绿，而防闪脚本会静默失效。
 */
test('切一套 ⇒ `data-theme` 挂上、盘上也写下（走真实 App 接线；守判据 2）', async () => {
  installFetch()
  render(<App />)
  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))
  await userEvent.click(screen.getByRole('menuitemradio', { name: '蒸汽波' }))

  // 两处都得断言：只验一处就守不住判据 2 的另一半——
  // `data-theme` 只活到这次页面为止，「刷新后仍在」靠的是盘上那份。
  expect(document.documentElement.getAttribute('data-theme')).toBe('vaporwave')
  expect(localStorage.getItem('vigil.theme')).toBe('vaporwave')
})

/**
 * ⭐⭐ M7 冒烟 F2 的**判据**（真缺陷：ARGUS 那个菜单项，点正中间点不中）。
 *
 * ## 冒烟实测的形态（`_smoke/m7-smoke-render.md` §4.1）
 * `ArgusLabel` 那个 `<span>` 的 `onClick` 里 `stopPropagation()` 的意图是
 * **"乱码期间点一下 = 揭晓，不是选中"**，但它此前是**无条件**的 ⇒ **揭晓之后仍然生效**
 * ⇒ 这个 span 继续吞掉点击，菜单项的 `onClick`（靠冒泡上来）永远收不到
 * ⇒ 点那一行**中心**（落在 span 上）**选不中 ARGUS**，可点区只剩按钮右侧约 23px 的留白。
 *
 * ## 为什么这是一条**真空**、必须补
 * `ThemePicker.dom.test.tsx` 此前**从来没有点过 argus 那一行的标签**：上面四条用例里
 * 最接近的一条点的是「蒸汽波」（普通文本菜单项）。⇒ "点标签 = 选中"这件事**零判据**，
 * `stopPropagation` 的宽窄**没有任何东西挡得住**（它就是"永远"这个更宽的行为）。
 *
 * ## 这条走**真实 `App` 接线**（理由同上一条：`data-theme` 与盘上那份都发生在
 * `App.pickTheme` 里，只 render `<ThemePicker onChange={...}/>` 守卫不到落盘那半，
 * 而"刷新后仍在"正是这里要钉的两个落点之二）。
 *
 * ⚠️ **第一次点击（乱码期）必须仍然不选中**——否则就是把 `stopPropagation` 一刀删掉、
 * 让"乱码期点一下顺手把主题选了"，那是把 F2 修成另一个缺陷。所以这条前置断言是承重的。
 * ⚠️ 变异反证（实测，原始输出见 `smoke-fix-report.md`）：把 `ArgusLabel` 的
 * `stopPropagation()` 改回**无条件** ⇒ 第二次点击不会冒泡 ⇒ 本条**必红**
 * （`data-theme` 停在 `default`、盘上那份是 `null`）。
 */
test('⭐⭐ F2：乱码期点标签=揭晓（不选中）；**揭晓之后**点标签=真的选中 ARGUS', async () => {
  installFetch()
  render(<App />)
  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))

  const label = screen.getByTestId('argus-label')

  // ① 乱码期：点它 = 揭晓，**不选中**（`stopPropagation` 在这一档仍然该生效）。
  await userEvent.click(label)
  expect(screen.getByText('Project ARGUS'), '乱码期点一下没揭晓').toBeTruthy()
  expect(document.documentElement.getAttribute('data-theme'), '乱码期点标签顺手把主题选了').toBeNull()
  expect(localStorage.getItem('vigil.theme')).toBeNull()
  expect(screen.queryByRole('menu'), '乱码期点标签把菜单关掉了（那是"选中"的行为，不是"揭晓"）').not.toBeNull()

  // ② 揭晓之后：点它 = **选中**——点击必须正常冒泡到菜单项。
  await userEvent.click(screen.getByTestId('argus-label'))
  expect(
    document.documentElement.getAttribute('data-theme'),
    '揭晓之后点标签选不中 ⇒ stopPropagation 还在吞点击（F2）',
  ).toBe('argus')
  expect(localStorage.getItem('vigil.theme'), '选中没落盘 ⇒ 刷新后主题回去了').toBe('argus')
  expect(screen.queryByRole('menu'), '选中之后菜单没收起').toBeNull()
})

/**
 * ⚠️ 同上，但走**自然揭晓**那条路（不点它，等 `ArgusLabel` 的定时器到点）——
 * 这正是冒烟里"等 3s 自然揭晓后再点中心"的那一下。
 *
 * 与上面那条不是重复：上面走的是"点击 ⇒ `setRevealed(true)`"，这条走的是
 * "定时器 ⇒ `setRevealed(true)`"。两条路都落到同一个 `revealed` 状态上，
 * 但**触发源不同**（一个是事件、一个是 effect 的 timeout），而 F2 的修复恰恰
 * 依赖那个状态，所以两条都钉一下。代价如实记：这条要真实等 `ms=2500`，约 2.6s。
 */
test('⭐⭐ F2（自然揭晓路径）：等它自己显名之后，点标签照样能选中 ARGUS', { timeout: 15000 }, async () => {
  installFetch()
  render(<App />)
  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))

  // ⚠️ 用 `findByText` 等**真名**出现（不点它）——`ArgusLabel` 默认 `ms=2500`。
  await screen.findByText('Project ARGUS', undefined, { timeout: 8000 })
  expect(localStorage.getItem('vigil.theme'), '还没点就已经选上了？这条的前置就不成立了').toBeNull()

  await userEvent.click(screen.getByTestId('argus-label'))
  expect(document.documentElement.getAttribute('data-theme')).toBe('argus')
  expect(localStorage.getItem('vigil.theme')).toBe('argus')
  expect(screen.queryByRole('menu')).toBeNull()
})
