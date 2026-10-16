import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import Calendar from '../views/Calendar.tsx'
import { _resetForTests, noteFetchResult } from '../connectivity.ts'
import { SNAPSHOT_STORAGE_KEY } from '../snapshot.ts'
import type { Item } from '../types.ts'

afterEach(() => {
  localStorage.clear()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  // ⚠️ 必须**最后**复位（`connectivity.ts` 的 `online` 是模块级状态）：
  // 顺序反了的话 `initial()` 读到的是被 stub 的 navigator。T5 新增的用例会
  // 派发 online/offline 事件，不复位就会跨用例串（与 Offline.dom.test.tsx 同一条注释）。
  _resetForTests()
})

function item(id: number, deadline: number | null): Item {
  return {
    item_id: id,
    kind: 'notice',
    kind_label: '通知公告',
    kind_icon: '📋',
    title: `条目${id}`,
    detail: null,
    event_ts: 1_700_000_000,
    deadline_ts: deadline,
    group_id: 111,
    group_name: '甲群',
    actor: null,
    actor_uin: null,
    place: null,
    amount: null,
    links: [],
    source_count: 0,
    start_ts: null,
  }
}

function installFetch(items: Item[]) {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/calendar')) return ok({ items, total: items.length })
      if (url.startsWith('/api/categories')) return ok({ categories: [] })
      return ok({})
    }),
  )
  return calls
}

/** 本机时区下某天的 epoch 秒。写测试时**不要硬编码时区**。 */
function at(y: number, mo: number, d: number): number {
  return Math.floor(new Date(y, mo - 1, d, 0, 0, 0).getTime() / 1000)
}

test('⚠️ 只标截止日、不显示时刻（spec §1.2 + G4）', async () => {
  // 固定"今天"到 2026-10，让 2026-10-20 是未来、2026-10-05 已过
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 20)), item(2, at(2026, 10, 5))])
  render(<Calendar />)
  await screen.findByText('2026年10月')
  const html = document.body.innerHTML
  // ⚠️ 变异反证：把 fmtDateTime 用进格子 ⇒ 本条必红
  expect(html).not.toContain('00:00')
})

test('点某天 ⇒ 列当天到期的条目', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 20)), item(2, at(2026, 10, 21))])
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 20 日/ }))
  expect(screen.getByText('条目1')).toBeTruthy()
  expect(screen.queryByText('条目2')).toBeNull()
})

test('⭐ 临近提醒：点那一天 ⇒ 显示「还有 N 天」并带分级钩子', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 13))]) // 3 天后 ⇒ soon
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 13 日/ }))
  expect(screen.getByText('还有 3 天')).toBeTruthy()
  // ⚠️ 读**语义钩子**而不是类名字面量（换 token / 调色不该让用例红）
  expect(
    screen.getByText('还有 3 天').getAttribute('data-deadline-level'),
  ).toBe('soon')
})

test('⭐ 临近提醒：明天截止 ⇒ urgent', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 11))])
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 11 日/ }))
  expect(screen.getByText('明天截止')).toBeTruthy()
})

test('⚠️ 超过一周的档位**不显示**相对文案（与左边的日期重复）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 30))]) // 20 天后 ⇒ normal
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 30 日/ }))
  expect(screen.getByText('条目1')).toBeTruthy()
  // ⚠️ normal 档由 `relDeadline` 返回绝对日期（与标题行左边的日期重复），
  // 所以组件**刻意不渲染**这个 span ⇒ 钩子必须不存在。
  expect(document.querySelector('[data-deadline-level]')).toBeNull()
})

test('⚠️ 空的那一天不显示催促文案（那是假话）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([]) // 一条都没有
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 13 日/ }))
  expect(screen.getByText('这一天没有到期的条目。')).toBeTruthy()
  expect(document.querySelector('[data-deadline-level]')).toBeNull()
})

test('⭐ 有 place 的条目 ⇒ 渲染地图链接（在按钮**外**）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  const withPlace = { ...item(1, at(2026, 10, 20)), place: '实验楼 305' }
  installFetch([withPlace])
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 20 日/ }))

  const link = screen.getByRole('link', { name: /在地图中查看/ })
  // ⚠️ 空格必须编码——不编码会被截断成「实验楼」，搜到别的地方
  expect(link.getAttribute('href')).toContain('305')
  expect(link.getAttribute('href')).not.toContain(' ')
  // ⚠️ `target="_blank"` 必须配 `rel` 里的 noopener（防反向操作本页）
  expect(link.getAttribute('rel')).toContain('noopener')
})

test('⚠️ 没有 place ⇒ **不渲染**地图链接（375 条里 255 条没有）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 20))]) // place: null
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 20 日/ }))
  expect(screen.getByText('条目1')).toBeTruthy()
  expect(screen.queryByRole('link', { name: /在地图中查看/ })).toBeNull()
})

// ── 冲突与重复（M7）────────────────────────────────────────────

/** 一个能带回冲突数据的 fetch stub（默认的 installFetch 只回 items）。 */
function installFetchWith(payload: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/calendar')) return ok(payload)
      if (url.startsWith('/api/categories')) return ok({ categories: [] })
      return ok({})
    }),
  )
}

test('⭐ 活动撞车 ⇒ 渲染红色横幅，且**两边都列出来**', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetchWith({
    items: [],
    total: 0,
    duplicates: {},
    conflicts: [
      {
        a: { label: '期末讲座', start: 1792000000, end: 1792005400, item_id: 1, kind: 'activity' },
        b: { label: '班级班会', start: 1792001800, end: 1792007200, item_id: 2, kind: 'activity' },
        minutes: 60,
      },
    ],
    course_conflicts: [],
  })
  render(<Calendar />)
  await screen.findByText('2026年10月')
  expect(screen.getByText(/1 个活动时间撞车/)).toBeTruthy()
  // ⚠️ **两边都要出现**：只说"冲突了"等于把排查丢回给用户
  expect(document.body.textContent).toContain('期末讲座')
  expect(document.body.textContent).toContain('班级班会')
  // ⚠️ **两侧的开始时刻都要显示**，而"重叠 N 分钟"**不许出现** ——
  // 那个数是从推算的活动时长算出来的，不是抽出来的（用户 2026-10-15 裁定）。
  // 变异反证：把 `c.minutes` 加回横幅 ⇒ 本条必红。
  expect(document.body.textContent).not.toContain('60 分钟')
})

test('⭐ 撞课 ⇒ 渲染**另一段**（warn 色），与活动撞车分开', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetchWith({
    items: [],
    total: 0,
    duplicates: {},
    conflicts: [],
    course_conflicts: [
      {
        a: { label: '期末讲座', start: 1792000000, end: 1792005400, item_id: 1, kind: 'activity' },
        b: { label: '大学英语', start: 1792000000, end: 1792006000, item_id: null, kind: 'course' },
        minutes: 90,
      },
    ],
  })
  render(<Calendar />)
  await screen.findByText('2026年10月')
  expect(screen.getByText(/1 处与你的课冲突/)).toBeTruthy()
  expect(document.body.textContent).toContain('大学英语')
})

test('⚠️ 课表读不出来 ⇒ 如实说，**不许静默什么都不显示**', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetchWith({
    items: [],
    total: 0,
    duplicates: {},
    conflicts: [],
    course_conflicts: [{ error: '课表读不出来，本页不报课冲突' }],
  })
  render(<Calendar />)
  await screen.findByText('2026年10月')
  // ⚠️ 静默空列表与"课表是空的、没有冲突"在界面上长得一样 —— 那是假话。
  expect(document.body.textContent).toContain('课表读不出来')
})

test('⚠️ 没有冲突 ⇒ **不渲染**任何横幅（阴性对照）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetchWith({ items: [], total: 0, duplicates: {}, conflicts: [], course_conflicts: [] })
  render(<Calendar />)
  await screen.findByText('2026年10月')
  expect(document.body.textContent).not.toContain('撞车')
  expect(document.body.textContent).not.toContain('与你的课冲突')
})

test('⭐ 重复条目 ⇒ **折叠显示**（不删除、不隐藏）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetchWith({
    items: [item(7, at(2026, 10, 20)), item(35, at(2026, 10, 20))],
    total: 2,
    duplicates: { '35': 7 }, // 35 是 7 的重复
    conflicts: [],
    course_conflicts: [],
  })
  render(<Calendar />)
  await screen.findByText('2026年10月')
  await userEvent.click(screen.getByRole('button', { name: /10 月 20 日/ }))
  // ⚠️ 代表那条**正常渲染**成卡片
  expect(screen.getByText('条目7')).toBeTruthy()
  // ⚠️ 重复那条**仍在 DOM 里**（不是被 filter 掉）—— 静默少一条不留痕迹，
  //    而那正是"静默丢数据"的形状。它带 `data-duplicate-of` 供测试与样式用。
  const dup = document.querySelector('[data-duplicate-of="7"]')
  expect(dup).not.toBeNull()
  expect(dup!.textContent).toContain('条目35')
})

test('⚠️ 快照回落时**不渲染**冲突横幅（我们不知道，不是"没有"）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  localStorage.setItem(
    SNAPSHOT_STORAGE_KEY,
    JSON.stringify({
      synced_at: Math.floor(new Date(2026, 9, 9, 8, 30).getTime() / 1000),
      feed: [], calendar: [item(1, at(2026, 10, 20))], digests: [],
      categories: [], persons: [],
    }),
  )
  vi.stubGlobal('fetch', vi.fn(async () => {
    throw new TypeError('Failed to fetch')
  }))
  render(<Calendar />)
  await screen.findByText(/离线/)
  // ⚠️ 快照里没有冲突信息 ⇒ `hasConflictData` 为 false ⇒ 不能说"没有冲突"。
  expect(document.body.textContent).not.toContain('撞车')
})

test('⚠️ 跨月翻页（12 月 → 次年 1 月）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 11, 15, 12, 0, 0)) // 2026-12
  installFetch([])
  render(<Calendar />)
  await screen.findByText('2026年12月')
  await userEvent.click(screen.getByRole('button', { name: '下个月' }))
  // ⚠️ 变异反证：把 `new Date(y, m + 1, 1)` 写成 `new Date(y + 1, m + 1, 1)` ⇒ 红
  expect(screen.getByText('2027年1月')).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: '上个月' }))
  expect(screen.getByText('2026年12月')).toBeTruthy()
})

test('⚠️ 网络失败且无快照 ⇒ 说"离线"而不是"没有条目"（G10）', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }),
  )
  render(<Calendar />)
  const msg = await screen.findByText(/离线/)
  expect(msg.textContent).toContain('还没有本地快照')
})

test('⚠️ 网络失败但有快照 ⇒ 显示快照 + 同步时刻', async () => {
  // ⚠️ 逃生舱修正（brief 的断言与 brief 自己的 Step 4 实现**不相容**）：
  // brief 原写 `getByText(/条目7|10 月 20 日/)`，但
  //   ① 条目标题只在**点了某天之后**才渲染（日历首屏无意列表）；
  //   ② "10 月 20 日" 只作为格子按钮的 **aria-label** 存在，不是文本节点。
  // ⇒ 按"验证快照数据真的渲染出来了 + 同步时刻可见"这个**原意**重写为下面两条。
  // 固定"今天"到 2026-10，否则这一条会随真实日期漂移（20 号可能翻出当前月）。
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  localStorage.setItem(
    SNAPSHOT_STORAGE_KEY,
    JSON.stringify({
      synced_at: Math.floor(new Date(2026, 9, 9, 8, 30).getTime() / 1000),
      feed: [],
      calendar: [item(7, at(2026, 10, 20))],
      digests: [],
      // ⚠️ T6 起这两份**元数据**是必需字段：缺了它们 `readSnapshot` 会当成
      // "没有快照"（半份快照更坏）。本文件只验证日历，留空数组即可。
      categories: [],
      persons: [],
    }),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }),
  )
  render(<Calendar />)
  const banner = await screen.findByText(/快照/)
  // 同步时刻**逐字**可见（"不许假装实时"——G10 / 出口标准 5）
  expect(banner.textContent).toContain('10月9日 08:30')
  // ⚠️⚠️ T6-F1：日历快照是**全量**（`syncSnapshot` 调 `fetchCalendar()` 无参 ⇒
  // `api_calendar` 缺省 = 全量带截止日的条目）。⇒ 横幅**不许**声称它被裁成了 5 天
  //（真库实测：全量 36 条横跨 4 个月，落进 5 天窗口的只有 5 条 ⇒ 写"最近 5 天"
  // 是写在用户界面上的**假话**）。
  //
  // ⚠️ 判据写成**否定式**（"不出现范围词"）而不是整句逐字：要的是"**不许声称
  // 范围**"这件事本身，整句逐字会被无关措辞改动（比如把"下面是"改成"显示的是"）
  // 假红。⚠️ 两条各钉一类假话：
  //   · `not.toContain('5 天')` —— 钉住我没有把 T6 那个"最近 5 天"写回来；
  //   · `not.toContain('最近')` —— 更宽地钉住"**任何**范围声称"（比如写成
  //     "最近 3 天""最近一个月"这种同样为假的说法）。
  // ⚠️ 变异反证：把「最近 5 天」加回 Calendar 横幅 ⇒ 上面两条**都**红（实测）。
  expect(
    banner.textContent,
    '日历快照是全量，横幅不许声称范围（如「最近 5 天」）——那是假话',
  ).not.toContain('5 天')
  expect(
    banner.textContent,
    '日历快照是全量，横幅不许出现任何"最近…"式的范围声称',
  ).not.toContain('最近')
  // 快照里的日历条目真的进了网格：10 月 20 日那格有一个到期点
  expect(screen.getByLabelText('1 条到期')).toBeTruthy()
})

/* ──────────────────────────────────────────────────────────────────────────
 * T4 加固（审查报出的两组 Important 覆盖缺口；brief 把测试面钉死了，故单开一轮）
 * ────────────────────────────────────────────────────────────────────────── */

/**
 * 取某一天格子里的"到期点"。用**语义**钩子 `data-overdue`，不读 `bg-vigil-fg-4`
 * 那个字面类名——换 token / 调色板时类名会变，而"过不过期"这件事不该跟着变。
 * 返回 undefined 表示那天没有点（没有截止日）。
 */
function dotOf(ariaLabel: string): HTMLElement | undefined {
  const cell = screen.getByRole('button', { name: ariaLabel })
  return within(cell).queryByLabelText(/条到期/) ?? undefined
}

test('⭐ 已过期的点**有**过期标记、未到期的点**没有**（用户 Q2：已过期以变淡区分）', async () => {
  // 今天 = 2026-10-10（冻结）。10-05 已过期、10-20 未到期。
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  installFetch([item(1, at(2026, 10, 5)), item(2, at(2026, 10, 20))])
  render(<Calendar />)
  await screen.findByText('2026年10月')

  const past = dotOf('10 月 5 日')
  const future = dotOf('10 月 20 日')
  expect(past, '10-05 应有到期点').toBeTruthy()
  expect(future, '10-20 应有到期点').toBeTruthy()
  // ⚠️ 变异反证：把 `k < today` 改成 `false` ⇒ `past` 的标记变 'false' ⇒ 本条红。
  expect(past?.getAttribute('data-overdue'), '已过期的那天必须被标成过期').toBe('true')
  expect(future?.getAttribute('data-overdue'), '未到期的那天不许被标成过期').toBe('false')

  // ⚠️ 变异反证：把过期点色 `bg-vigil-fg-4` 改成 `bg-vigil-accent`
  // ⇒ 两点**同一套类名** ⇒ 下面这条红。
  // ⚠️ 判据是"两者**不同**"（= 用户 Q2 的「变淡区分」这个契约），
  // **不是**"等于哪个字面类名"——后者换个 token 就假红。
  expect(past?.className, '过期与未到期的点必须**视觉可区分**（变淡）').not.toBe(future?.className)
})

test('⭐ 三态①：成功时**不许**挂快照横幅、**不许**弹错误框（通·无数据）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  // 盘上**放一份快照**：成功路径若也读了它 / 也挂了横幅，下面两条就能抓到。
  localStorage.setItem(
    SNAPSHOT_STORAGE_KEY,
    JSON.stringify({
      synced_at: Math.floor(new Date(2026, 9, 9, 8, 30).getTime() / 1000),
      feed: [],
      calendar: [item(99, at(2026, 10, 20))],
      digests: [],
      // ⚠️ T6 起这两份**元数据**是必需字段：缺了它们 `readSnapshot` 会当成
      // "没有快照"（半份快照更坏）。本文件只验证日历，留空数组即可。
      categories: [],
      persons: [],
    }),
  )
  // 网络**通**，且返回**空**（三态里的第一条：通·无数据）
  installFetch([])
  render(<Calendar />)
  await screen.findByText('2026年10月')

  // ⚠️ 变异反证 X2：成功路径也 `setSnapshotAt(...)` ⇒ `快照` 横幅出现 ⇒ 本条红
  await waitFor(() => expect(screen.queryByText(/快照/), '成功时不许挂快照横幅').toBeNull())
  // ⚠️ 变异反证 X7：成功路径也 `setErr(...)` ⇒ `日历读取失败` 出现 ⇒ 本条红
  expect(screen.queryByText(/日历读取失败/), '成功时不许弹错误框').toBeNull()
  // 阴性对照：这一天**没有**条目 ⇒ 网格里一个到期点都没有（网络数据是空的那份）
  expect(screen.queryByLabelText(/条到期/)).toBeNull()
})

test('⭐ 三态②：有快照但网络通 ⇒ 显示**网络**数据、不挂横幅（X8：不许优先取快照）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  // ⚠️ X8 的关键：**快照与网络数据必须不同**，否则测不出"优先取谁"。
  //   快照：10-05 一条（标记 item_id=888 不在）——用一个**不同的日子**当指纹。
  localStorage.setItem(
    SNAPSHOT_STORAGE_KEY,
    JSON.stringify({
      synced_at: Math.floor(new Date(2026, 9, 9, 8, 30).getTime() / 1000),
      feed: [],
      calendar: [item(888, at(2026, 10, 5))],
      digests: [],
      // ⚠️ T6 起必需（见上一条同形注释）。
      categories: [],
      persons: [],
    }),
  )
  // 网络：只有 10-20 一条（快照里**没有**这个日子）
  installFetch([item(7, at(2026, 10, 20))])
  render(<Calendar />)
  await screen.findByText('2026年10月')

  // ⚠️ 变异反证 X8：`.then` 里也 `readSnapshot()` 并 setItems(snap.calendar)
  // ⇒ 屏幕上出现的是**快照那份**（10-05 有点、10-20 没点）⇒ 下面两条红。
  await waitFor(() => expect(dotOf('10 月 20 日'), '应显示网络数据(10-20 有点)').toBeTruthy())
  expect(dotOf('10 月 5 日'), '快照那份(10-05)不许出现').toBeUndefined()
  expect(screen.queryByText(/快照/), '网络通时不挂横幅').toBeNull()
})

/* ──────────────────────────────────────────────────────────────────────────
 * 终审 F1/F2（跨任务的缝：`online` 这条契约在 Calendar 上从未被消费）
 * ────────────────────────────────────────────────────────────────────────── */

/** 盘上一份快照，日历里只有 10-05 这一天（一个**可辨认的指纹**）。 */
function writeCalendarSnapshot() {
  localStorage.setItem(
    SNAPSHOT_STORAGE_KEY,
    JSON.stringify({
      synced_at: Math.floor(new Date(2026, 9, 9, 8, 30).getTime() / 1000),
      feed: [],
      calendar: [item(888, at(2026, 10, 5))],
      digests: [],
      // ⚠️ T6 起必需（见上一条同形注释）。
      categories: [],
      persons: [],
    }),
  )
}

/** 一个**活着但每个请求都 404** 的假后端（F1 的入口：4xx 保持 `online===true`）。 */
function stub404(detail = '订单不存在') {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ detail }) })),
  )
}

/** fetch **本身抛** = 网络层没通（真离线）。返回值同 `installFetch`：收到的 URL 列表。 */
function stubDead() {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input))
      throw new TypeError('Failed to fetch')
    }),
  )
  return calls
}

test('⭐ F1：在线 4xx + 盘上有快照 ⇒ **不许说"离线"**、错误框要在', async () => {
  /**
   * ⭐ F1 —— T5-F2 那个缺陷在**第四个消费者**上被漏掉：`api.ts` 对 4xx 保持
   * `online===true`，而 `Calendar` 全文不 import connectivity ⇒ 屏幕把
   * "读不到"（后端活着）谎报成"离线，且还没有本地快照。"。
   *
   * ⚠️ 前置：jsdom 的 `navigator.onLine` 默认 true，且 404 会 `noteFetchResult(true)`
   * ⇒ `online` 保持真（这正是 F1 的入口：失败 **!=** 离线）。
   *
   * ⚠️ 变异反证：把 Calendar 的 `!online` 守卫删掉（回到"只要有 snapshotAt 就挂横幅"）
   * ⇒ `queryByText(/离线/)` 非空 ⇒ 本条必红。
   */
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  writeCalendarSnapshot()
  stub404('日历炸了')
  render(<Calendar />)

  // ⭐ 在线 4xx **仍是错误** ⇒ 错误框在场（不许因为回落到快照就把在线错误吞掉）
  expect(await screen.findByText(/日历读取失败/)).toBeTruthy()
  expect(screen.getByText(/日历炸了/)).toBeTruthy()
  // ⭐ 快照照常回落（fallback 由"失败"决定，不由"离线"决定）
  expect(screen.getByLabelText('1 条到期'), '快照那份(10-05)照常渲染').toBeTruthy()
  // ⭐ 屏幕上**不许**出现任何"离线"字样
  expect(screen.queryByText(/离线/), '在线 4xx 不许说"离线"').toBeNull()
})

test('⭐ F1 阴性对照：**真离线**失败 ⇒ 横幅与"离线"句确实出现（不是"恒不出现"）', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  writeCalendarSnapshot()
  stubDead()
  render(<Calendar />)

  // 有快照 ⇒ 横幅必须说清"何时同步的"（出口标准 5）
  expect(await screen.findByText(/快照/)).toBeTruthy()
  expect(screen.getByText(/10月9日 08:30/)).toBeTruthy()
})

test('⭐ F1 阴性对照：真离线且**无**快照 ⇒ 说"离线，且还没有本地快照"', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  stubDead()
  render(<Calendar />)
  expect(await screen.findByText(/离线，且还没有本地快照/)).toBeTruthy()
})

test('⭐ F2：离线→在线 ⇒ **自动重取**、回到网络数据、横幅消失（出口标准 4）', async () => {
  /**
   * ⭐ F2 —— 恢复在线只被 `Feed` 消费；Calendar 的取数 effect 原本只依赖
   * `[reloadTick]`，**不听 online 边沿** ⇒ 网络恢复后**一直卡在快照上**
   * （审查实测 `banner1=banner2=true`）。出口标准 4 因此不成立。
   *
   * ⚠️ 快照与网络数据必须**不同**（10-05 vs 10-20 两个日子当指纹），否则测不出
   * "到底重取没有"。
   * ⚠️ 判据一次钉两件事：① 重取到了网络数据 ② 横幅消失 —— 后者正是出口标准 5
   * 要的"有快照数据 ⇔ 有横幅"（不许出现"实时数据 + 快照横幅"或反过来）。
   *
   * ⚠️ 变异反证：把 `[reloadTick, onlineEpoch]` 改回 `[reloadTick]`（或不加
   * `onlineEpoch`）⇒ 网络恢复后屏幕仍是 10-05 的快照、横幅还在 ⇒ 本条必红
   *（两处断言各自都红）。
   */
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date(2026, 9, 10, 12, 0, 0))
  writeCalendarSnapshot()
  // 离线挂载（用户离线打开 PWA —— 出口标准 2/3 的场景本身）
  _resetForTests()
  noteFetchResult(false)
  stubDead()
  render(<Calendar />)
  expect(await screen.findByText(/快照/), '先落到快照上').toBeTruthy()
  expect(dotOf('10 月 5 日'), '快照那份(10-05)已渲染').toBeTruthy()

  // 恢复：换一个活的后端，再派发浏览器的 online 事件
  installFetch([item(7, at(2026, 10, 20))])
  await act(async () => {
    window.dispatchEvent(new Event('online'))
  })

  await waitFor(() => expect(dotOf('10 月 20 日'), '应重取到网络那份(10-20)').toBeTruthy())
  expect(dotOf('10 月 5 日'), '快照那份(10-05)该被换掉').toBeUndefined()
  expect(screen.queryByText(/快照/), '回到实时后横幅必须消失').toBeNull()
})

test('⭐ F2：**离线挂载** ⇒ 那个 `online &&` 合取项是承重的：不许白发第二次首屏', async () => {
  /**
   * ⭐⭐ `onlineEpoch` 的判据是 `if (online && !wasOnline)`——**两半都承重**。
   * 这一条钉的是 `online &&` 那一半（上一条钉的是 `!wasOnline` 那一半）。
   *
   * ⚠️ 关键前提是**离线挂载**（用户离线时打开 PWA —— 出口标准 2/3 的场景本身）：
   * 此时 `useRef(online)` 的初值就是 `false` ⇒ 派生 effect 第一次跑时
   * `wasOnline === false`。
   *   · 根治版 `if (online && !wasOnline)`：当前也离线 ⇒ 为假 ⇒ **不**+1
   *   · 只留 `!wasOnline` 版：`!wasOnline` 为真 ⇒ **+1** ⇒ 把取数 effect 多跑一次
   *     （一次必然失败的请求）。
   *
   * ⚠️ 必须**先 `noteFetchResult(false)` 再 render**：否则挂载那一刻
   * `navigator.onLine`（jsdom 默认 true）让 `online===true`，两版**同绿**
   * ——这正是 Feed 那边上一轮误判"不可观测"的由来。
   *
   * ⚠️ 变异反证：`if (online && !wasOnline)` → `if (!wasOnline)` ⇒ 本条必红（请求数 2）。
   */
  _resetForTests()
  noteFetchResult(false) // 离线：挂载时 online 为 false
  const calls = stubDead()
  render(<Calendar />)
  expect(await screen.findByText(/离线，且还没有本地快照/)).toBeTruthy()
  // 给"派生 effect"足够时间走完（它若 +1，取数 effect 会再发一次）
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
  expect(calls.filter((u) => u.startsWith('/api/calendar')).length).toBe(1)
})
