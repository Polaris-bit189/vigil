import { StrictMode, useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { _resetForTests } from '../connectivity.ts'
import { fmtDateTime } from '../format.ts'
import { writeSnapshot } from '../snapshot.ts'
import { VIEW_STORAGE_KEY } from '../uiState.ts'
import Digests from '../views/Digests.tsx'
import Feed from '../views/Feed.tsx'
import type { CategoryDetail, DigestDetail, Item } from '../types.ts'

/**
 * ⭐ T6：**离线时类目条 / 人物筛选器从快照来** + **离线本地筛**。
 *
 * 用户 2026-10-10 裁定:**类目条保留 + 本地筛**。
 * 缺陷原话(人眼验收时发现):
 *   「离线快照里的**信息流页中的类目选项会直接消失**,导致无法通过点类目来筛选。」
 *
 * 根因:`Feed` 的类目条从 `cats` 渲染,`cats` 来自 `fetchCategories()`;
 * 离线时失败 ⇒ `setCats([])` ⇒ 只剩「全部」。
 * ⭐ **同一个洞的第二个消费者**:`PersonPicker` 的名单来自 `fetchPersons()`,
 * 离线时 `people=null` ⇒ **人物筛选器整块消失**。两处一起修(本项目刚吃过
 * "只修我知道的那几处、漏掉第 N 个消费者"的亏 —— E13)。
 *
 * ⚠️ 本文件与 `Offline.dom.test.tsx` **故意分开**:那边是 M5/T5 已有的离线守卫,
 * 这里是 T6 新增的"元数据 + 本地筛"这一层。夹具也不同(那边是单类目,
 * 这里必须**多类目**才验得动本地筛)。
 *
 * ⚠️ `_resetForTests()` 必须(不是洁癖):`connectivity.ts` 的 `online` 是
 * **模块级**状态,`noteFetchResult(false)` 会跨用例串(顺序:先撤 stub、再复位)。
 */
afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  localStorage.removeItem(VIEW_STORAGE_KEY)
  _resetForTests()
})

// ── 夹具 ────────────────────────────────────────────────────────────────

const SNAPSHOT_TS = Math.floor(new Date(2026, 9, 9, 8, 30).getTime() / 1000)
/** `fmtDateTime(SNAPSHOT_TS)` === '10月9日 08:30'(不硬编码时区)。 */
const SNAPSHOT_LABEL = fmtDateTime(SNAPSHOT_TS)

/** 两个类目,用来验"类目条不止「全部」"与"点一个只剩该类目"。 */
function cat(slug: string, label: string, icon: string): CategoryDetail {
  return { slug, label, icon, count: 1, desc: `${label}的判据` }
}

/** 能指定 kind / actor_uin / detail 的条目工厂(本地筛三种维度都要有东西可筛)。 */
function t6Item(
  id: number,
  kind: string,
  actorUin: number | null,
  title: string,
  detail: string | null = null,
): Item {
  return {
    item_id: id,
    kind,
    kind_label: kind,
    kind_icon: '📌',
    title,
    detail,
    event_ts: 1_700_000_000,
    deadline_ts: null,
    group_id: 111,
    group_name: '甲群',
    actor: actorUin === null ? null : `用户${actorUin}`,
    actor_uin: actorUin,
    place: null,
    amount: null,
    links: [],
    source_count: 0,
    start_ts: null,
  }
}

/** 一份**多类目、多人物**的快照(四条:notice/academic/life/life-匿名)。 */
function writeT6Snapshot() {
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [
      t6Item(1, 'notice', 10001, '停水通知', '周三全楼停水'),
      t6Item(2, 'academic', 10001, '选课通知'),
      t6Item(3, 'life', 10002, '宿舍洗衣房', '洗衣机坏了'),
      t6Item(4, 'life', null, '匿名失物招领'),
    ],
    calendar: [],
    digests: [],
    categories: [
      cat('notice', '通知公告', '📋'),
      cat('academic', '学业', '📚'),
      cat('life', '生活', '🍜'),
    ],
    persons: [
      { uin: 10001, label: '辅导员张老师', note: '', count: 2 },
      { uin: 10002, label: '宿管阿姨', note: '', count: 1 },
    ],
  })
}

/** 一个**全失败**的假后端:fetch 本身抛 = 网络层没通(离线)。 */
function stubDeadApi(): string[] {
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

/**
 * 一个**活着但每个请求都 404** 的假后端(在线 4xx:后端在线、只是这个请求有问题)。
 * ⚠️ 它**不是** `stubDeadApi`:4xx 走 `noteFetchResult(r.status < 500)` = `true`
 * ⇒ `online` 保持真。这正是"在线失败要不要本地筛"那条阴性对照的入口。
 */
function stub404Api(): string[] {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input))
      return { ok: false, status: 404, json: async () => ({ detail: '订单不存在' }) }
    }),
  )
  return calls
}

/** 静态 props 的 Feed(筛选条件不可变)——给"只看类目条/横幅"那几条用。 */
const feedProps = {
  kinds: [] as string[],
  onKinds: () => {},
  persons: [] as number[],
  onPersons: () => {},
  reloadSignal: 0,
}

/**
 * 一个**受控**的 `Feed`:把 `kinds`／`persons` 提到外层 state,
 * 这样"点一个类目 chip"才真的会改变筛选条件(`feedProps` 里的 setter 是空函数)。
 */
function FeedHarness() {
  const [kinds, setKinds] = useState<string[]>([])
  const [persons, setPersons] = useState<number[]>([])
  return (
    <Feed
      kinds={kinds}
      onKinds={setKinds}
      persons={persons}
      onPersons={setPersons}
      reloadSignal={0}
    />
  )
}

// ── ① 元数据从快照来(缺陷本身)────────────────────────────────────────

test('⭐ T6：离线 ⇒ 类目条**不只「全部」**(从快照取类目),不是消失', async () => {
  /**
   * ⚠️ 缺陷本身:离线时 `fetchCategories()` 失败 ⇒ `setCats([])` ⇒ 屏幕上只剩
   * 「全部」,用户点不出任何类目。
   * ⚠️ 变异反证:把 `setCats(isOnline() ? [] : readSnapshot()?.categories ?? [])`
   * 改回 `setCats([])` ⇒ 「学业」「生活」两个 chip 都不出现 ⇒ 本条必红。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(<Feed {...feedProps} />)
  await screen.findByText('停水通知') // 先把快照落稳
  // 类目条从快照填出来了
  expect(screen.getByRole('button', { name: /通知公告/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /学业/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /生活/ })).toBeTruthy()
  // ⚠️ 阴性对照:「全部」一直都在(它是 `kinds.length === 0` 那个 chip,
  // **不**来自 cats)——所以"三个类目 chip 出现"才是承重的证据。
  expect(screen.getByRole('button', { name: '全部' })).toBeTruthy()
})

test('⭐ T6：离线 ⇒ 人物筛选器**有名单**(从快照取 persons),不是空名单/消失', async () => {
  /**
   * ⚠️ 第二个消费者:离线时 `fetchPersons()` 失败 ⇒ `people=null` ⇒
   * `people !== null && people.length > 0` 那一支不渲染 ⇒ 名单整块消失。
   * ⚠️ 变异反证:把 `PersonPicker` 里"离线回落 `snap.persons`"删掉 ⇒
   * 打开面板只剩"读取中…" ⇒ 本条必红。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(<Feed {...feedProps} />)
  await screen.findByText('停水通知')
  await userEvent.click(screen.getByRole('button', { name: /按人物筛选/ }))
  expect(await screen.findByText(/辅导员张老师/)).toBeTruthy()
  expect(screen.getByText(/宿管阿姨/)).toBeTruthy()
})

// ── ② 离线本地筛(用户要的效果)─────────────────────────────────────────

test('⭐ T6：离线 + 点一个类目 ⇒ 屏幕上**只剩该类目**的条目(本地筛快照)', async () => {
  /**
   * ⭐ 这是用户要的效果。快照是**多类目**的(notice / academic / life),
   * 点「生活」后屏幕上只该剩 `kind === 'life'` 的两条。
   * ⚠️ 变异反证:去掉 `filterFeed`(回落时直接用 `snap.feed`) ⇒ 四条都在 ⇒ 本条必红。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(<FeedHarness />)
  await screen.findByText('停水通知')

  await userEvent.click(screen.getByRole('button', { name: /生活/ }))
  // 只剩 life 的两条
  expect(await screen.findByText('宿舍洗衣房')).toBeTruthy()
  expect(screen.getByText('匿名失物招领')).toBeTruthy()
  expect(screen.queryByText('停水通知'), 'notice 那条该被筛掉').toBeNull()
  expect(screen.queryByText('选课通知'), 'academic 那条该被筛掉').toBeNull()
  // 计数行说的是**筛后**条数(不是快照总条数)
  expect(screen.getByText(/共 2 条/)).toBeTruthy()
})

test('⭐ T6：离线 + 人物筛选 ⇒ 本地按 actor_uin 筛(匿名条目不匹配)', async () => {
  /**
   * ⚠️ 人物维度的口径(与后端 `sn.uin` 对齐):匿名条目 `actor_uin === null`
   * **必不匹配**任何人物——它不是"某个人的匿名帖",它是没有署名。
   * ⚠️ 变异反证:把 `persons.includes(it.actor_uin ?? NaN)` 那一行删掉 ⇒
   * 选张老师后"匿名失物招领"仍会留在屏幕上 ⇒ 本条必红。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(<FeedHarness />)
  await screen.findByText('停水通知')

  await userEvent.click(screen.getByRole('button', { name: /按人物筛选/ }))
  await userEvent.click(await screen.findByRole('button', { name: /辅导员张老师/ }))

  // 张老师的条目:notice(停水通知) + academic(选课通知)
  expect(await screen.findByText('停水通知')).toBeTruthy()
  expect(screen.getByText('选课通知')).toBeTruthy()
  expect(screen.queryByText('宿舍洗衣房'), '别人的条目该被筛掉').toBeNull()
  expect(screen.queryByText('匿名失物招领'), '匿名条目不匹配任何人物').toBeNull()
})

test('⭐ T6：离线 + 关键词 ⇒ 本地子串筛(title/detail 都算)', async () => {
  /**
   * ⚠️ 口径:不分词、**子串包含**、**大小写敏感**——与后端 FTS5 trigram 的
   * 长查询**不完全一致**(本地更严格,可能少给不会多给;见 `Feed.tsx` 的 `filterFeed`)。
   * 这条钉的是"关键词本地筛真的在工作",以及 **detail 也进匹配范围**。
   * ⚠️ 变异反证:把 `filterFeed` 里 `q` 那一支删掉 ⇒ 关键词不筛 ⇒ 本条必红。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(<FeedHarness />)
  await screen.findByText('停水通知')

  const box = screen.getByPlaceholderText(/搜标题或详情/)
  await userEvent.type(box, '停水') // debounce 300ms ⇒ 用 waitFor 等筛生效
  await waitFor(() => expect(screen.queryByText('选课通知'), '关键词筛后只剩停水那条').toBeNull())
  expect(screen.getByText('停水通知')).toBeTruthy()
  expect(screen.queryByText('宿舍洗衣房')).toBeNull()

  // detail 也进匹配:搜「洗衣机」(只在 life 那条的 detail 里)
  await userEvent.clear(box)
  await userEvent.type(box, '洗衣机')
  await waitFor(() => expect(screen.queryByText('停水通知'), 'detail 也进匹配').toBeNull())
  expect(screen.getByText('宿舍洗衣房')).toBeTruthy()
})

test('⭐ T6：离线 + 筛选筛不出任何条目 ⇒ 说"当前筛选…下还没有条目",不说"离线无快照"', async () => {
  /**
   * ⚠️ 三态依旧要分开:离线 + 有快照 + 这个筛选筛出 0 条 ⇒ 是"这个筛选下没有",
   * **不是**"离线且还没有本地快照"、**也不是**"读取失败"。
   * ⚠️ 变异反证:回落那支漏掉"筛后为空也算有快照"(把 `items.length > 0`
   * 写成 `snap.feed.length > 0`)⇒ 会走 else 支 ⇒ 屏幕上变成"读取失败" ⇒ 本条必红。
   */
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [t6Item(1, 'notice', 10001, '停水通知')],
    calendar: [],
    digests: [],
    categories: [cat('notice', '通知公告', '📋'), cat('academic', '学业', '📚')],
    persons: [],
  })
  stubDeadApi()
  render(<FeedHarness />)
  await screen.findByText('停水通知')

  // 点一个快照里**一条都没有**的类目
  await userEvent.click(screen.getByRole('button', { name: /学业/ }))
  expect(await screen.findByText(/当前筛选.*下还没有条目/)).toBeTruthy()
  expect(screen.queryByText(/读取失败/), '筛不到不是读取失败').toBeNull()
  expect(screen.queryByText(/离线，且还没有本地快照/)).toBeNull()
  // 横幅仍要在(手里这份**是**快照)
  expect(screen.getByText(/仅最近 5 天/)).toBeTruthy()
})

// ── ③ 范围要诚实(快照只含最近 5 天)───────────────────────────────────

test('⭐ T6：离线 + 带类目筛选 ⇒ 横幅出现,且**含范围词「仅最近 5 天」**', async () => {
  /**
   * ⚠️ 快照只含最近 5 天 ⇒ "筛不到"会被读成"没有这类"。
   * 横幅**必须**点明范围(「只说自己有资格说的话」,M2)。
   * ⚠️ 变异反证:删掉横幅里的「仅最近 5 天」⇒ 本条必红。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(<FeedHarness />)
  await screen.findByText('停水通知')
  await userEvent.click(screen.getByRole('button', { name: /生活/ }))
  await screen.findByText('宿舍洗衣房')

  // ⚠️ 原文断言(不是正则):范围词一旦被删就红。
  expect(
    screen.getByText(`离线：显示的是本地快照（${SNAPSHOT_LABEL} 同步，仅最近 5 天）。`),
  ).toBeTruthy()
  expect(screen.getByText(/仅最近 5 天/)).toBeTruthy()
})

test('⭐ T6：Digests 离线横幅也含范围词「仅最近 5 篇」', async () => {
  /**
   * ⚠️ 范围词在三处快照横幅上都要有(Feed/Calendar/Digests)——只修自己知道的那一处,
   * 就是 E13 那个"漏掉第 N 个消费者"的复现。
   * ⚠️ 变**篇**不是**天**:日报一天一篇,但快照存的是"最近 5 篇"(`SNAPSHOT_DIGESTS`)。
   */
  const digest: DigestDetail = {
    digest_id: 9,
    day: '2026-10-05',
    window_from: 1_700_000_000,
    window_to: 1_700_086_400,
    created_at: 1_700_090_000,
    item_count: 1,
    body_md: '快照里的日报正文。',
    items: [],
  }
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [],
    calendar: [],
    digests: [digest],
    categories: [],
    persons: [],
  })
  stubDeadApi()
  render(<Digests />)
  await screen.findByText('2026-10-05')
  expect(
    screen.getByText(`离线：显示的是本地快照（${SNAPSHOT_LABEL} 同步，仅最近 5 篇）。`),
  ).toBeTruthy()
})

// ── ④ 反向把守:在线时行为**一字不许改** ───────────────────────────────

test('⭐ T6【阴性对照】：**在线** + 盘上有快照 + 带筛选失败(4xx) ⇒ 仍如实报错,**绝不本地筛**', async () => {
  /**
   * ⭐ 反向把守:把本地筛**误用在在线**是最容易发生的过度改动——
   * 那时"筛不到"可能是后端真的出错了,拿本地快照冒充答案会把一次真实的
   * 失败谎报成一次成功的结果(M2 的既有裁决)。
   *
   * ⚠️ 变异反证:把 `canFallback` 里的 `isOnline() ? ... : true` 写成恒 `true`
   * (即"在线也本地筛")⇒ 快照那条 notice 会留在屏幕上、"读取失败"消失 ⇒ 必红。
   * ⚠️ 与 `Offline.dom.test.tsx` 的「在线 4xx + 带筛选 ⇒ 如实报错」**故意重复**:
   * 那条是"不回落";这条额外要求"即使盘上有**匹配筛选**的快照,也不许拿它顶上"。
   */
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [t6Item(1, 'notice', 10001, '快照里的停水通知')],
    calendar: [],
    digests: [],
    categories: [cat('notice', '通知公告', '📋')],
    persons: [],
  })
  stub404Api() // 在线(4xx 保持 online===true)且每个请求都失败
  render(<Feed {...feedProps} kinds={['notice']} />)

  expect(await screen.findByText(/读取失败/)).toBeTruthy()
  expect(
    screen.queryByText('快照里的停水通知'),
    '在线失败不许拿快照冒充筛选结果',
  ).toBeNull()
  expect(screen.queryByText(/本地快照（/), '在线不许挂快照横幅').toBeNull()
  expect(screen.queryByText(/离线/), '在线 4xx 不许说"离线"').toBeNull()
})

test('⭐ T6：**在线**成功 ⇒ 类目条从**网络**来,不读快照的类目(阴性对照)', async () => {
  /**
   * ⚠️ 反向把守:快照里放一份**与网络不同**的类目表,在线成功时屏幕上必须
   * 是**网络**那一份。否则"离线回落"会被写成"无条件读快照"
   * (与 X8「不许优先取快照」同一条判据,只是换到了类目表上)。
   */
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [],
    calendar: [],
    digests: [],
    categories: [cat('stale', '快照里的旧类目', '🧟')], // 指纹
    persons: [],
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body })
      if (url.startsWith('/api/categories')) {
        return ok({ categories: [cat('notice', '通知公告', '📋')] })
      }
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      if (url.startsWith('/api/items')) return ok({ items: [], total: 0, limit: 50, offset: 0 })
      return ok({})
    }),
  )
  render(<Feed {...feedProps} />)
  await screen.findByText('还没有任何条目。')
  expect(screen.getByRole('button', { name: /通知公告/ })).toBeTruthy()
  expect(
    screen.queryByRole('button', { name: /快照里的旧类目/ }),
    '在线成功时不许出现快照那份类目',
  ).toBeNull()
})

// ── ⑤ 在线 4xx：类目/人物也**不许**回落(两个 catch 的守卫各一条)────────────
//
// ⭐ 这一组是**"反着再问一句"**抓到的缺口:上面四条在线用例都只让
// **items** 那个请求以"带筛选"的方式失败 ⇒ 它们钉的是 `canFallback`，
// 而 Feed 类目 effect 与 PersonPicker 的 catch 里各还有一条 `isOnline()` 守卫
// **没有任何用例经过**——把这两条守卫删掉，全量测试仍绿(我实测过)。
// 这里补两条独立的阴性对照把它们钉住。

test('⭐ T6【阴性对照】：**在线** 4xx 失败 ⇒ 类目条**不许**回落快照那份(不谎报成功)', async () => {
  /**
   * ⚠️ Feed 的类目 effect:`catch` 里读快照填 `cats` 这一步**必须**由
   * `isOnline()` 把关。在线 4xx(后端活着、请求有问题)时回落到一张旧类目表
   * = 把一次**失败**谎报成一次**成功**(与 F2/F3 的既有裁决同一条判据)。
   *
   * ⚠️ 与"在线成功"那条**故意不同**:那条走的是 **`.then` 成功路径**
   * (根本没有 catch),钉的是"成功时不读快照";这条走的是 **`catch` 离线回落
   * 那一支**,钉的是"失败时**在线**也不读快照"。两条各钉一半,缺一不可。
   * ⚠️ 变异反证:把 `setCats(isOnline() ? [] : (readSnapshot()?.categories ?? []))`
   * 写成 `setCats(readSnapshot()?.categories ?? [])` ⇒ 「快照里的旧类目」出现 ⇒ 必红。
   */
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [],
    calendar: [],
    digests: [],
    categories: [cat('stale', '快照里的旧类目', '🧟')], // 指纹
    persons: [],
  })
  stub404Api() // 在线(4xx 保持 online===true)且每个请求都失败
  render(<Feed {...feedProps} />)
  // 先等错误框(证明确实走到了 catch)
  await screen.findByText(/读取失败/)
  // ⭐ 在线 4xx ⇒ 不许出现快照那份类目
  expect(
    screen.queryByRole('button', { name: /快照里的旧类目/ }),
    '在线 4xx 失败时不许拿快照的类目冒充',
  ).toBeNull()
})

test('⭐ T6【阴性对照】：**在线** 4xx 失败 ⇒ PersonPicker **不许**回落快照名单', async () => {
  /**
   * ⚠️ PersonPicker 的 `catch` 里读快照填 `people` 同样必须由 `isOnline()` 把关
   * ——与上一条同一条判据,只是换了个组件(第二个消费者)。
   * ⚠️ 变异反证:把 `const snap = isOnline() ? null : readSnapshot()` 写成
   * `const snap = readSnapshot()` ⇒ 「快照里的人物」出现 ⇒ 必红。
   */
  writeSnapshot({
    synced_at: SNAPSHOT_TS,
    feed: [],
    calendar: [],
    digests: [],
    categories: [],
    persons: [{ uin: 99999, label: '快照里的人物', note: '', count: 1 }], // 指纹
  })
  stub404Api()
  render(<Feed {...feedProps} />)
  await userEvent.click(screen.getByRole('button', { name: /按人物筛选/ }))
  // 面板打开了(「新增」按钮恒在)——再断言名单里没有快照那一份。
  // ⚠️ 不用 `findByText(/订单不存在/)`:Feed 与 PersonPicker 会**各显示一条**
  // 同文案的错误,`findByText` 会因为 multiple matches 抛(测试脆弱,非行为错)。
  await screen.findByRole('button', { name: '新增' })
  expect(
    screen.queryByText(/快照里的人物/),
    '在线 4xx 失败时不许拿快照的人物名单冒充',
  ).toBeNull()
})

test('⭐ T6：StrictMode(挂→卸→挂)下离线仍能从快照取到类目(订阅/挂载没被搞坏)', async () => {
  /**
   * ⚠️ T6 给 `Feed` 的类目 effect 加了一条 `catch` 里的 `readSnapshot()`
   * ——这一步必须与 StrictMode 的"挂→卸→挂"相容(两次挂载各读一次快照,
   * 是幂等的本地读)。这条钉的就是它不炸、结果还在。
   */
  writeT6Snapshot()
  stubDeadApi()
  render(
    <StrictMode>
      <Feed {...feedProps} />
    </StrictMode>,
  )
  await screen.findByText('停水通知')
  expect(screen.getByRole('button', { name: /学业/ })).toBeTruthy()
})
