import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import App from '../App.tsx'
import ArgusBrand from '../components/ArgusBrand.tsx'
import ThemePicker from '../components/ThemePicker.tsx'
import { clearArgusColor } from '../argusColor.ts'
import { VIEW_STORAGE_KEY } from '../uiState.ts'

/**
 * `App` 会渲染 `Feed` ⇒ 一挂载就发请求。桩法与 `App.dom.test.tsx:8` 的
 * `installFetch` **同一套**，只多了三条**形状正确**的空壳（digests/groups/llm）：
 * 本文件要点 `[3]日报` / `[4]配置`——那里若拿到 `{}`，组件会读到 `undefined`
 * 再 `.map`/取字段，**报错与被测行为毫无关系**（本仓「桩的契约要对」那条老账）。
 */
function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/undo')) return ok({ undone: false })
      if (url.startsWith('/api/categories/detailed')) return ok({ categories: [] })
      if (url.startsWith('/api/categories')) return ok({ categories: [] })
      if (url.startsWith('/api/digests')) return ok({ digests: [] })
      if (url.startsWith('/api/groups')) return ok({ groups: [], available: true })
      if (url.startsWith('/api/llm')) return ok({})
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      if (url.startsWith('/api/items')) return ok({ items: [], total: 0, limit: 50, offset: 0 })
      return ok({})
    }),
  )
}

// ⚠️ `vigil.theme` 在盘上会**真的**被 `applyTheme` 写（App 的 `pickTheme` 走它）
// ⇒ 逐条清干净，否则下一条是"从上一个用例留下的主题开跑"（M6 #27 的同族形态）。
// ⚠️ 行内变量也清：ARGUS 的头部用例会经 `applyTheme('argus')` 往 `:root` 上贴颜色。
afterEach(() => {
  localStorage.clear()
  clearArgusColor()
  document.documentElement.removeAttribute('data-theme')
  vi.unstubAllGlobals()
})

/**
 * ⚠️ **R14 的语义变更**（不是放宽断言）：字标从 `<div>ARGUS</div>`（文本节点）换成了
 * `role="img"` + `aria-label="ARGUS"` 的**蒙版图形** ⇒ `getByText('ARGUS')` **本该**失配
 * （图形里没有文字节点），改查 **role/label**。
 * 判据的**强度不变**：仍然是"这一处必须能被无障碍读到名字 ARGUS"，
 * 而且下面那条 `role=img` 的断言**同时钉住了"它是个图形"**——
 * 若有人把它退回文本节点，`getByRole('img', {name:'ARGUS'})` 会红。
 */
test('品牌区三件套：徽记 + ARGUS 字标 + 小字', () => {
  render(<ArgusBrand />)
  expect(screen.getByRole('img', { name: 'ARGUS 徽记' })).toBeTruthy()
  expect(screen.getByRole('img', { name: 'ARGUS' })).toBeTruthy()
  expect(screen.getByText('睡亦警觉，眼观六路')).toBeTruthy()
})

test('⛔ 徽记必须是**纯色**（全部 currentColor/无填充色）——这是"logo 颜色随主题色同步"的**唯一保证**', () => {
  // 这条不是格式洁癖：只要有一处写死了颜色，/color 改了主题色而 logo 不同步，
  // 而**没有任何别的东西会红**（真浏览器里才看得出来）。
  const { container } = render(<ArgusBrand />)
  const svg = container.querySelector('svg')!
  for (const el of Array.from(svg.querySelectorAll('*'))) {
    for (const attr of ['stroke', 'fill', 'color']) {
      const v = el.getAttribute(attr)
      if (v !== null) expect(['none', 'currentColor'], `${attr}=${v}`).toContain(v)
    }
  }
})

/**
 * ⭐⭐ R14（T8-2 收尾的美术落地）：**字标那一半的同一条「纯色」纪律**。
 *
 * 用户原话：「品牌区那里的"argus"就用这个吧，**但注意它要能随主题色切换而变色，
 * 所以这个图片不能直接用**。」⇒ 落点是「**位图当蒙版**」：形状来自资产
 * （`mask-image`），颜色来自 `currentColor`（Tailwind 的 `bg-current`）。
 *
 * ⚠️ 为什么非要一条判据：那个颜色来源**只是一个类名**。删掉它 ⇒ 蒙版没有背景色，
 * 屏幕上**字标整块消失**；换成任何写死的颜色类（或干脆塞进 `<img>`）⇒ 颜色被**烤死**，
 * `/color` 改主题色而字标**纹丝不动**。这两种情况**都没有别的东西会红**——
 * 真浏览器里才看得出来（正是上一条注释说的同一件事）。
 */
test('⛔ 字标必须是**蒙版 + bg-current**（颜色随主题色走），且**不许**有写死颜色的类', () => {
  const { container } = render(<ArgusBrand />)
  const mark = container.querySelector<HTMLElement>('[role="img"][aria-label="ARGUS"]')
  expect(mark, '找不到字标元素（role="img" + aria-label="ARGUS"）').toBeTruthy()
  const cls = mark!.className

  // ① 存在性：颜色来源必须是 **`bg-current`**，⛔ **不认 `text-current`**（F-B1）。
  //    ⭐⭐ 实测（T8-2 复审，2026-09-22）：把 `bg-current` 换成 `text-current`
  //    ⇒ **19 files / 141 tests 全绿**，而真浏览器里字标会**整块消失**：
  //    `text-current` 只设 `color`，而蒙版读的是 `background-color`
  //    （这个 div 里没有文字节点，`color` 等于没设）⇒ 备选那条**放行了一个看不见的实现**。
  //    ⚠️ "无效"只是描述，"**无效但全绿**"才是危害——它让这条**唯一**守着"字标看得见"的
  //    判据变成一条假绿通道。所以备选删掉，只留 `bg-current`。
  //    ⚠️ 误伤面≈0：本判据只查 `[role="img"][aria-label="ARGUS"]` 这**一个**元素；
  //    ③ 已强制该元素必须是蒙版 ⇒ 将来若有人改成内联 `background-color: currentColor`，
  //    那是**有意的实现变更**，本就该回来改判据。
  expect(
    /\bbg-current\b/.test(cls),
    `字标没有 bg-current ⇒ 蒙版没有颜色来源，字标会整块消失（text-current 只设 color，不算）：${cls}`,
  ).toBe(true)

  // ② 全称否定（与上一条的"纯色"守卫同款）：**除了 `*-current`，不许有任何别的颜色类**。
  // ⚠️ 写成"凡 `bg-`/`text-`/… 开头的类名都必须以 `-current` 收尾"，而不是去列举调色板名
  // ——本仓 `noRawPalette.test.ts` 的扫描会把**本文件里**写出来的任何调色板类名字面量
  // 当成真命中（它只豁免它自己），列举法会把自己变成一处违规。
  const COLORISH = /^(?:bg|text|border|ring|fill|stroke|from|to|via|decoration|accent|caret)-/
  const offenders = cls.split(/\s+/).filter((t) => COLORISH.test(t) && !/(?:bg|text)-current$/.test(t))
  expect(offenders, `字标上有写死颜色/非 currentColor 的类（颜色会被烤死、不再随主题走）：${offenders}`).toEqual([])

  // ③ 阴性对照：蒙版确实挂**在这个元素**上（否则 ①② 可以靠一个空的 `div` 混过去——
  //    那正是本仓「全称否定必须有存在性对照」的老账）。
  const style = mark!.getAttribute('style') ?? ''
  expect(style, '字标元素上没有 mask-image ⇒ 它根本不是蒙版').toMatch(/mask-image\s*:/)
  expect(style, 'mask-image 没指向那份美术资产').toContain('argus-wordmark')
})

test('阴性对照：徽记的 `stroke` 不是"恰好没写"——根元素**显式**用它，否则上一条可以靠空 svg 混过去', () => {
  // ⚠️ 上面那条「徽记必须是纯色」是**全称否定**（"没有任何字面色值"），
  // 而空 svg / 不画线的 svg 也满足它 ⇒ 没有这一条，把 `stroke="currentColor"` 删掉
  //（或整个 svg 删空）照样全绿，而屏幕上的徽记**是一条都看不见的空白**。
  // 这是本仓「全称否定必须有存在性对照」的老账。
  const { container } = render(<ArgusBrand />)
  const svg = container.querySelector('svg')!
  expect(svg.getAttribute('stroke')).toBe('currentColor')
  // ⚠️ 计数按 D 版（计划 R13，2026-09-22 用户定稿）改写：三个**空环** + 六条线
  //（中干 / 两条弧线扇骨 / 三条支干），不再是旧版的 2×2 眼睛矩阵。
  // ⭐ 上面那条是**全称否定**，这里就是它的**存在性对照**：数**精确**值而不是"至少几个"
  // ——`toBeGreaterThanOrEqual` 挡不住"删掉两条扇骨但还剩 4 条"这种只掉一块的回归。
  expect(svg.querySelectorAll('circle'), '三个环').toHaveLength(3)
  expect(svg.querySelectorAll('path'), '中干+两扇骨+三支干').toHaveLength(6)
  // ⭐ 三个环必须是**空**环（用户定稿的第③条：把球掏空成环）。
  // ⚠️ 加这条的理由：根元素的 `fill="none"` 是**继承**下来的，而上面那条"纯色"守卫
  // 只要求 `fill ∈ {none, currentColor}` ⇒ 给某个 circle 加一个 `fill="currentColor"`
  // 会让它**悄悄变回实心球**（= 用户明确否掉的旧造型），而**全量一条不红**。
  expect(svg.getAttribute('fill'), '环靠这个继承下来才是空的').toBe('none')
  for (const c of Array.from(svg.querySelectorAll('circle'))) {
    const f = c.getAttribute('fill')
    expect(f === null || f === 'none', `环被填成了实心球：fill=${f}`).toBe(true)
  }
})

/**
 * ⭐ 头部替换（计划 Task 2 Step 3）：`theme === 'argus'` ⇒ 换掉整个 header。
 *
 * ⚠️ 起渲染的方式照计划 Step 5 写死的那条：`localStorage.setItem('vigil.theme','argus')`
 * ——`App` 的 `useState(readTheme)` 是惰性初始化，挂载时读一次盘。
 */
test('ARGUS 头部：品牌区**整体取代**「🏮 VIGIL」+ `ARGUS:~$` 提示符', async () => {
  localStorage.setItem('vigil.theme', 'argus')
  installFetch()
  render(<App />)
  expect(screen.getByRole('img', { name: 'ARGUS 徽记' })).toBeTruthy()
  // ⚠️ 字标查 role/label（R14 语义变更，见文件首条用例的注释）
  expect(screen.getByRole('img', { name: 'ARGUS' })).toBeTruthy()
  expect(screen.getByText('睡亦警觉，眼观六路')).toBeTruthy()
  expect(screen.getByText('ARGUS:~$')).toBeTruthy()
  expect(screen.queryByText('🏮 VIGIL'), 'ARGUS 下旧品牌区必须整体让位').toBeNull()
  // ⚠️ `/color` 的输入行必须在**头部**就活着（它是本主题的命令入口，不是某个页面里的控件）
  expect(screen.getByLabelText('ARGUS 命令')).toBeTruthy()
})

test('ARGUS 头部：`[1]…[4]` 四个方括号导航都在，且**点它们真的切页**', async () => {
  localStorage.setItem('vigil.theme', 'argus')
  installFetch()
  render(<App />)
  for (const label of ['[1]信息流', '[2]类目', '[3]日报', '[4]配置']) {
    expect(screen.getByText(label), `少了 ${label}`).toBeTruthy()
  }
  // ⚠️ 判据用**落盘的 tab**（`writeView` 是 App 的既有出口）而不是"某个按钮多了个类名"：
  // 前者是"页面真的换了"的机械证据，后者只说明"样式变了"。
  const tab = () => JSON.parse(localStorage.getItem(VIEW_STORAGE_KEY) ?? '{}').tab
  await userEvent.click(screen.getByText('[2]类目'))
  await waitFor(() => expect(tab()).toBe('cats'))
  await userEvent.click(screen.getByText('[3]日报'))
  await waitFor(() => expect(tab()).toBe('digests'))
  await userEvent.click(screen.getByText('[4]配置'))
  await waitFor(() => expect(tab()).toBe('settings'))
  await userEvent.click(screen.getByText('[1]信息流'))
  await waitFor(() => expect(tab()).toBe('feed'))
})

test('阴性对照：非 ARGUS 主题下**不许**出现品牌区（它是这套主题的身份，其余五套保持「🏮 VIGIL」）', () => {
  installFetch() // 盘上没写主题 ⇒ `readTheme()` 回落 'default'
  render(<App />)
  expect(screen.queryByRole('img', { name: 'ARGUS 徽记' })).toBeNull()
  // ⚠️ 字标也是品牌区的一件（R14 换成了图形）⇒ 它也必须在别的主题下**整体让位**；
  // 少了这一条，字标漏到其余五套主题里不会有任何信号。
  expect(screen.queryByRole('img', { name: 'ARGUS' })).toBeNull()
  expect(screen.queryByText('睡亦警觉，眼观六路')).toBeNull()
  expect(screen.queryByText('ARGUS:~$')).toBeNull()
  expect(screen.getByText('🏮 VIGIL')).toBeTruthy()
  // 默认主题的导航是**没有方括号**的（两套头部不是"同一套加了个括号"）
  expect(screen.getByText('信息流')).toBeTruthy()
  expect(screen.queryByText('[1]信息流')).toBeNull()
})

/**
 * ⭐ I4（delta 终审 Important）：`ArgusCommand` 是**第二个写入口**，而它"只在 argus 下渲染"
 * 此前**没有判据**。
 *
 * 为什么这条非有不可：`ArgusCommand` 直接调 `applyArgusColor`（**不经** `applyTheme`），
 * 于是它绕开了 `syncArgusColor` 那层"只在 argus 主题下写"的门（`theme.ts` 的注释把这一点
 * 写得很清楚：「直接调它而在非 argus 主题下就是 §0.1 那个"行内覆盖顶掉别的主题"的缺陷
 * 换个入口重演」）。今天它**正确**，靠的是 `App.tsx:71` 那个三元——`theme === 'argus'`
 * 才渲染这套头部。⚠️ 而上面那条阴性对照守的是**品牌区**（徽记 / 字标 / 小字 / `ARGUS:~$`），
 * 它**够不到命令行**：有人把 `<ArgusCommand />` 挪进默认头部 ⇒ 另外五套主题下多出一个
 * 能改 ARGUS 颜色的输入行，而**零信号**。
 *
 * ⚠️ 变异反证：把 `App.tsx` 的 argus 分支去掉（两套头部合并、总是渲染带命令行的那个）
 * ⇒ 本条**必红**（第二段拿不到 `ARGUS 命令`，或第一段不该有却有了）。
 */
test('⛔ 非 ARGUS 主题下**不渲染** `ArgusCommand`（它只在终端头部里活着）', () => {
  installFetch()
  // ① 非 argus（盘上没写主题 ⇒ `readTheme()` 回落 'default'）：命令行**一格都不许有**。
  const first = render(<App />)
  expect(
    screen.queryByLabelText('ARGUS 命令'),
    '非 ARGUS 主题下出现了命令行 ⇒ 那是个能改 ARGUS 颜色的第二写入口',
  ).toBeNull()
  first.unmount()

  // ② 换到 argus（`App` 的 `useState(readTheme)` 是惰性初始化 ⇒ 挂载时读盘）：它必须**在**。
  localStorage.setItem('vigil.theme', 'argus')
  render(<App />)
  expect(
    screen.getByLabelText('ARGUS 命令'),
    'ARGUS 主题下没有命令行 ⇒ `/color` 的入口没了（功能不许少）',
  ).toBeTruthy()
})

test('`undoMsg` 在 ARGUS 头部里也有落点（终端风的一行小字）——功能不许少', async () => {
  localStorage.setItem('vigil.theme', 'argus')
  installFetch()
  render(<App />)
  await userEvent.click(screen.getByText('↶undo'))
  expect(await screen.findByText('没有可撤销的操作')).toBeTruthy()
})

/**
 * ⭐ Task 3 的钩子 —— ⚠️⭐ I3（delta 终审 Important）：**它的消费者从来没有出现**，这里照实写。
 *
 * `data-theme-picker` 是 T8-2 为 T8-3 留的接口（原注释："那里要在这块浮层上挂乱码演出"），
 * 而 T8-3 **没有用它**——乱码标签渲染在菜单项内部。全仓（含 `_smoke/` 的脚本）
 * 除了「`ThemePicker.tsx` 加属性」与「本文件这条判据」，**没有第三处**。
 * ⇒ 这条判据**钉住的是一个没人用的属性**——本仓对这件事的态度是"要么它有用、要么照实说"，
 * 所以：① 属性的注释已改成"今天没有消费者"；② 这条判据改成**照实守它是什么**——
 * 它守的是"菜单容器有一个稳定的机械标识，且菜单的行为一字未改"，**不再声称**有人要用它。
 * ⛔ 要退役它：属性与这条判据**一起删**（那是一次有意的接口退役，不是回归）。
 */
test('ThemePicker 的菜单容器带 `data-theme-picker` 钩子，且行为一字未改', async () => {
  const onChange = vi.fn()
  render(<ThemePicker value="argus" onChange={onChange} />)
  expect(document.querySelector('[data-theme-picker]'), '菜单没开时不该有这个节点').toBeNull()

  await userEvent.click(screen.getByRole('button', { name: '切换主题' }))
  const menu = document.querySelector('[data-theme-picker]')
  expect(menu, '菜单开着却找不到 data-theme-picker 钩子').toBeTruthy()
  expect(menu!.querySelectorAll('[role="menuitemradio"]')).toHaveLength(6)

  await userEvent.click(screen.getByRole('menuitemradio', { name: '毛玻璃' }))
  expect(onChange).toHaveBeenCalledWith('glass')
})
