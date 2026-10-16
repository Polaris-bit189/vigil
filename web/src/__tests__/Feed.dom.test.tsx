import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import Feed from '../views/Feed.tsx'
import type { Item } from '../types.ts'

const ITEM: Item = {
  item_id: 7,
  kind: 'notice',
  kind_label: '通知公告',
  kind_icon: '📋',
  title: '周三停水',
  detail: null,
  event_ts: 1_700_000_000,
  deadline_ts: null,
  group_id: 111,
  group_name: '甲群',
  actor: null,
  actor_uin: null,
  place: null,
  amount: null,
  links: [],
  source_count: 1,
  start_ts: null,
}

type Call = { url: string; method: string }

/**
 * 装一个**有状态**的假 fetch。
 *
 * ⚠️ 必须 mock 全局 `fetch` 而**不是** mock `../api.ts`：本文件要钉的缺陷
 * 恰恰是「**组件有没有再发一次请求、请求长什么样**」。
 * mock 掉 api 层就把被测的那一层换掉了，测试会退化成
 * 「我 mock 的函数被我调用了」——与真实行为无关（M4「设置对了 ≠ 行为对了」同族）。
 */
function installFetch(initial: Item[]) {
  let alive = [...initial]
  const calls: Call[] = []

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ url, method })

      const ok = (body: unknown) => ({
        ok: true,
        status: 200,
        json: async () => body,
      })

      if (url.startsWith('/api/categories')) {
        return ok({ categories: [{ slug: 'notice', label: '通知公告', icon: '📋', count: 1, desc: 'x' }] })
      }
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      if (method === 'DELETE' && url.startsWith('/api/items/')) {
        alive = []
        return ok({ edit_id: 1 })
      }
      if (url.startsWith('/api/items/')) return ok({ ...ITEM, sources: [] })
      if (url.startsWith('/api/items')) {
        return ok({ items: alive, total: alive.length, limit: 50, offset: 0 })
      }
      return ok({})
    }),
  )
  return calls
}

afterEach(() => vi.unstubAllGlobals())

test('⭐ 有活动开始时刻 ⇒ 显示「🕐 …」，且**不显示结束时刻**', async () => {
  /** ⚠️ 用户 2026-10-15 裁定「只显示开始时间排表」。
   *
   * 结束时刻是**推算的**（抽取没给时长，我们按 90 分钟假设），
   * 把它渲染成区间等于把猜测当事实 —— 而界面上没有任何痕迹提示那是猜的。
   * 开始时刻是**真的**（来自源文、过了 `start_supported` 闸门）。
   */
  const started = Math.floor(new Date(2026, 9, 15, 14, 0).getTime() / 1000)
  installFetch([{ ...ITEM, start_ts: started }])
  render(
    <Feed kinds={[]} onKinds={() => {}} persons={[]} onPersons={() => {}} reloadSignal={0} />,
  )
  expect(await screen.findByText(/🕐/)).toBeTruthy()
  expect(document.body.textContent).toContain('14:00')
  // ⚠️ 变异反证：把 `start_ts + 90*60` 也渲染出来 ⇒ 本条必红
  expect(document.body.textContent).not.toContain('15:30')
})

test('⚠️ 没有活动时刻 ⇒ **不渲染**时钟（为 null 是常态）', async () => {
  installFetch([ITEM]) // start_ts: null
  render(
    <Feed kinds={[]} onKinds={() => {}} persons={[]} onPersons={() => {}} reloadSignal={0} />,
  )
  await screen.findByText('周三停水')
  expect(document.body.textContent).not.toContain('🕐')
})

test('删掉一条之后，它必须从列表里消失（而不是被"往后追加"留在原地）', async () => {
  /**
   * ⭐ M5 A-Minor-7 缺陷 1 的守卫。
   *
   * ⚠️ 两个断言**都要有**，它们钉的是两件不同的事：
   *   ① 「条目消失」——用户看到的结果；
   *   ② 「重取的请求里没有 offset」——机制。
   * 只有 ① 的话，一个「删完把本地 state 摘掉」的实现也能绿，
   * 而那种实现遇到分页/撤销就会错；只有 ② 的话，请求对了但渲染没跟上照样绿。
   *
   * ⚠️ 变异反证：把 `Feed.tsx` 里的 `onMutated={reload}` 改回
   * `onMutated={retry}` ⇒ 本条**必红**（`retry()` 会走 `loadMore()`，
   * 而它带 `offset=1`，删掉的条目仍留在列表里）。
   */
  const calls = installFetch([ITEM])
  render(
    <Feed kinds={[]} onKinds={() => {}} persons={[]} onPersons={() => {}} reloadSignal={0} />,
  )

  expect(await screen.findByText('周三停水')).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: '删除' }))

  await waitFor(() => {
    expect(screen.queryByText('周三停水')).toBeNull()
  })

  const after = calls.filter((c) => c.url.startsWith('/api/items'))
  const last = after[after.length - 1]
  expect(last.url).not.toContain('offset'), (
    '删除后的重取带了 offset——那是 loadMore 的追加语义，' +
    '它不会刷新屏幕上那条已经变了的条目（M5 实测的两个缺陷之一）'
  )
})
