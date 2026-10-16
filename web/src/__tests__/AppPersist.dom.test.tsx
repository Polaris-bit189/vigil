import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import App from '../App.tsx'
import { VIEW_STORAGE_KEY } from '../uiState.ts'

afterEach(() => {
  localStorage.clear()
  vi.unstubAllGlobals()
})

function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/categories')) return ok({ categories: [] })
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      // ⚠️ `/api/digests` 这一条**计划里漏了**，必须补：落到下面那个 `ok({})` 兜底
      // ⇒ `r.digests` 是 `undefined` ⇒ `Digests.tsx:187` 的 `list.length` 抛
      // TypeError（实测：React 把整棵树卸掉，`<body>` 里只剩两个空 div）
      // ⇒ 那条红与被测行为（"刷新后 tab 还在不在"）毫无关系，它是**桩不全**。
      if (url.startsWith('/api/digests')) return ok({ digests: [] })
      if (url.startsWith('/api/items')) return ok({ items: [], total: 0, limit: 50, offset: 0 })
      return ok({})
    }),
  )
}

test('切到「日报」再重挂载 ⇒ 仍在日报（"刷新即重置"的守卫）', async () => {
  installFetch()
  const first = render(<App />)
  await userEvent.click(screen.getByRole('button', { name: '日报' }))
  // ⚠️ 变异反证：把初始化改回 `useState<Tab>('feed')` ⇒ 本条必红。
  expect(localStorage.getItem(VIEW_STORAGE_KEY)).toContain('digests')
  first.unmount()

  render(<App />)
  // ⚠️ 判据按**实测**落定（brief 的逃生舱）：日报页首屏渲染出的那一句，是空列表
  // 文案「还没有日报。生成：`vigil digest`」（`Digests.tsx:188`）。
  //
  // ⚠️ brief 里写的 `/日报/` **不能用**，实测它两头都错：
  //   ① **恒真**——tab 按钮自身的文字就是「日报」，与当前在哪个 tab 无关；
  //      （所以 `findByText('日报')` 精确串匹配同样恒真：它只命中那个按钮。）
  //   ② **必红**——它同时命中「日报」按钮和「还没有日报。生成：」两句，
  //      `findByText` 直接报 "Found multiple elements"（实测，两次命中都打印出来了）。
  // 一条恒真的断言与一条必红的断言同价（M5「空守卫」的同族），所以收窄成
  // **只有日报页才会渲染**的那半句：tab 若回落信息流，屏幕上只有
  // 「还没有任何条目。」⇒ find 超时 ⇒ 必红。
  expect(await screen.findByText(/还没有日报/)).toBeTruthy()
})

test('⚠️ 存了非法 tab ⇒ 回落 feed，不许白屏', async () => {
  installFetch()
  localStorage.setItem(
    VIEW_STORAGE_KEY,
    JSON.stringify({ tab: 'gallery', kinds: [], persons: [] }),
  )
  render(<App />)
  expect(await screen.findByText('还没有任何条目。', undefined, { timeout: 2000 })).toBeTruthy()
})
