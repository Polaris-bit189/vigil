import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import App from '../App.tsx'
import { VIEW_STORAGE_KEY } from '../uiState.ts'

type Call = { url: string; method: string }

function installFetch(undoResult: boolean) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, method: init?.method ?? 'GET' })
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/undo')) return ok({ undone: undoResult })
      if (url.startsWith('/api/categories')) return ok({ categories: [] })
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      if (url.startsWith('/api/items')) return ok({ items: [], total: 0, limit: 50, offset: 0 })
      return ok({})
    }),
  )
  return calls
}

/**
 * ⭐ T5 review 的 Low-2（fix 轮）：view 持久化上线后，`render(<App/>)` 会**从盘上**
 * 读初始 tab/筛选（`uiState.readView`),而这里原先的 `afterEach` **不清 localStorage**
 * ⇒ 下一个用例是**从上一个用例留下的那个页面开跑**的（M6 #27「模块级状态跨用例串」的
 * 同族缺陷：单条绿、整文件一起跑就换了个起点）。本文件当下每条的起点恰好都是默认值，
 * 所以今天是零红——它是**可构造的假绿**，不是历史问题。
 * ⚠️ 这里**可以**用 `VIEW_STORAGE_KEY` 常量：它不是与 `index.html` 防闪脚本的接口
 *（盘上那个键名只有 `uiState.ts` 一个真相）。必须用**字面量**的是
 * `ThemePicker.dom.test.tsx` 里那条断言——键名在两处（TS 与防闪脚本），
 * 用常量就守不住"两处没漂移"。 */
afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.removeItem(VIEW_STORAGE_KEY)
})

test('撤销成功后必须让筛选列表重取一次', async () => {
  /**
   * ⭐ M5 A-Minor-7 缺陷 2 的守卫。
   *
   * ⚠️ 变异反证：把 `App.tsx` 的 `if (r.undone) setDataTick((t) => t + 1)`
   * 删掉 ⇒ 本条**必红**。
   *
   * ⚠️ `before` 必须取在**首个响应返回之后**（M6 终审 #20）：从前的写法是
   * `waitFor(() => calls.some('/api/items'))` —— 那只保证请求**发出去了**，
   * 而它 `.then` 里的 `setState` 还没跑。于是"首屏那份数据落地"引起的重渲染
   * 有可能落在 `before` 之后 ⇒ 这一条与它的阴性对照
   * （「撤销没撤成时不许白白重取一次」）**互为自校验**的那对关系被削弱。
   * 判据改成等一句**只有数据回来之后才会渲染**的话（空列表的文案）：
   * 它一出现，首屏那次请求就一定已经 resolve 过了。
   */
  const calls = installFetch(true)
  render(<App />)
  await screen.findByText('还没有任何条目。', undefined, { timeout: 2000 })
  const before = calls.filter((c) => c.url.startsWith('/api/items')).length

  await userEvent.click(screen.getByRole('button', { name: /撤销/ }))

  await waitFor(() => {
    expect(calls.filter((c) => c.url.startsWith('/api/items')).length).toBeGreaterThan(before)
  }, { timeout: 2000 })
})

test('撤销**没撤成**时不许白白重取一次（阴性对照）', async () => {
  /**
   * ⭐ 这条是上一条的**阴性对照**，缺了它上一条就没有说服力。
   *
   * ⚠️ 没有它的话，「无条件 `setDataTick`」这个实现照样能让上一条绿——
   * 而那样每次点「撤销」都会白拉一次列表，包括栈空的时候
   * （而栈空恰恰是最常被点到的状态：用户点一下看看有没有东西可撤）。
   * 这与本项目「假绿」的判据是同一条：**一个只在"应该发生"时才发生的守卫，
   * 必须同时验"不该发生时它没发生"。**
   */
  const calls = installFetch(false)
  render(<App />)
  await waitFor(() =>
    expect(calls.some((c) => c.url.startsWith('/api/items'))).toBe(true),
  )
  const before = calls.filter((c) => c.url.startsWith('/api/items')).length

  await userEvent.click(screen.getByRole('button', { name: /撤销/ }))
  expect(await screen.findByText('没有可撤销的操作')).toBeTruthy()

  await new Promise((r) => setTimeout(r, 300))
  expect(calls.filter((c) => c.url.startsWith('/api/items')).length).toBe(before)
})
