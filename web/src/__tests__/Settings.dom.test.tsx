import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import App from '../App.tsx'
import { VIEW_STORAGE_KEY } from '../uiState.ts'
import Settings from '../views/Settings.tsx'
import type { CategoriesResponse, GroupsResponse, LlmSettings } from '../types.ts'

type Call = { url: string; method: string; body?: unknown }

type Fake = {
  groups?: GroupsResponse
  categories?: CategoriesResponse
  /** `POST /api/categories/undo` 的状态码（默认 200）。 */
  undoStatus?: number
  /** `POST /api/persons` 的响应（默认 201 成功）。 */
  personPost?: { status: number; body: unknown }
  /** `POST /api/config/pack` 的响应（默认 200 + 一个最小成功体）。 */
  packPost?: { status: number; body: unknown }
  /** `GET /api/llm` 的回值。 */
  llm?: LlmSettings
  /** `PUT /api/llm` 的回值（默认与 GET 同一份）；刻意可以**不同**——见那条回填用例。 */
  llmPut?: LlmSettings
  /** 让 `POST /api/groups` **挂住不返回**（用来观察提交中的 busy 态）。 */
  holdGroupPost?: boolean
}

const DEFAULT_LLM: LlmSettings = {
  base_url: 'https://x/v1',
  model: 'm',
  enable_thinking: null,
}

/**
 * 装一个**有状态**的假 fetch（骨架照抄 T8 的 `Feed.dom.test.tsx`）。
 *
 * ⚠️ 必须 mock 全局 `fetch` 而**不是** mock `../api.ts`：本文件要钉的缺陷
 * 恰恰是「组件有没有把后端给的东西显示出来、有没有真的发那一次请求、
 * 请求里装的是什么」。mock 掉 api 层就把被测的那一层换掉了，测试会退化成
 * 「我 mock 的函数被我调用了」——与真实行为无关。
 *
 * ⚠️ `Call.body` 也记下来（Fix loop 1）：`apply` / `keep_settings` 这类
 * **请求体里的契约**只有把它记下来才断言得到；只看 URL 的话，
 * 一个永远发 `apply: false` 的实现照样绿。
 */
function installFetch(f: Fake) {
  const calls: Call[] = []
  let release: (() => void) | null = null
  const held = f.holdGroupPost
    ? new Promise<void>((r) => {
        release = r
      })
    : null

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = init?.body === undefined ? undefined : JSON.parse(String(init.body))
      calls.push({ url, method, body })
      const ok = (b: unknown, status = 200) => ({ ok: status < 400, status, json: async () => b })
      const fail = (status: number, detail: string) => ({
        ok: false,
        status,
        json: async () => ({ detail }),
      })

      if (method === 'POST' && url === '/api/groups') {
        if (held) await held
        return ok({ id: 444 })
      }
      if (method === 'POST' && url === '/api/persons') {
        const r = f.personPost ?? { status: 201, body: { uin: 1, label: 'x' } }
        return ok(r.body, r.status)
      }
      if (url.startsWith('/api/persons')) return ok({ persons: [] })
      if (method === 'POST' && url.endsWith('/api/categories/undo')) {
        if (f.undoStatus && f.undoStatus >= 400) {
          return fail(f.undoStatus, '没有可撤销的类目改动')
        }
        return ok({ undone: true })
      }
      if (method === 'POST' && url === '/api/config/pack') {
        const r = f.packPost ?? { status: 200, body: { applied: true } }
        return ok(r.body, r.status)
      }
      if (url.startsWith('/api/config/pack')) {
        // ⚠️ 导出那份 TOML 与用例里"上传的包"**刻意不同**：相同的话
        // `getByDisplayValue` 会同时命中两个 textarea，红出来的原因是
        // "找到多个元素"——一条与被测行为毫无关系的失败。
        return ok({ toml: '# pack\n[[groups]]\nid = 1\n' })
      }
      if (url === '/api/llm' && method === 'PUT') return ok(f.llmPut ?? f.llm ?? DEFAULT_LLM)
      if (url.startsWith('/api/llm')) return ok(f.llm ?? DEFAULT_LLM)
      if (url.startsWith('/api/groups')) {
        return ok(f.groups ?? { available: true, reason: '', generated_at: 0, groups: [] })
      }
      if (url.startsWith('/api/categories') && method !== 'GET') {
        // POST（新增）/ PUT（编辑）/ DELETE（删除）都走这一支：本文件里
        // 这三条只需要一个成功的响应，**真正被断言的是发出去的 URL 与请求体**。
        return ok({ slug: 'ok' }, 201)
      }
      if (url.startsWith('/api/categories')) {
        return ok(f.categories ?? { categories: [], aliases: {} })
      }
      if (url.startsWith('/api/items')) return ok({ items: [], total: 0, limit: 50, offset: 0 })
      if (url.startsWith('/api/digests')) return ok({ digests: [] })
      return ok({})
    }),
  )
  return { calls, release: () => release?.() }
}

// ⚠️ **`URL.createObjectURL` / `revokeObjectURL` 必须自己还**（Fix loop 2 / Minor-1）：
// jsdom 没有实现这两个方法，而"下载"那条用例要把替身**直接赋在全局 `URL` 上**
// ——`vi.restoreAllMocks()` 与 `vi.unstubAllGlobals()` **都管不着直接赋值**
// （前者只还原 `vi.spyOn` 造的 mock，后者只还原 `stubGlobal` 造的）。
// reviewer 实测：那条用例跑完之后它**仍然是一个 vitest mock**，
// 而同一条用例里 `vi.spyOn` 出来的 `document.createElement` 已经还原成真的
// ——**同一个 `afterEach` 里两件事的待遇不同**，只因为一处是 spy、一处是赋值。
// 今天零红；将来会以"找不到元素 / 返回 undefined"的怪样现形（它是个 mock 函数，
// 默认返回 `undefined`，而调用方拿到的是一个**看起来像 URL 的假值**）。
//
// ⚠️ 存下原值再写回（而不是 `delete`）：将来 jsdom 真实现了它们，这条也照原样还。
const ORIG_URL_METHODS = {
  createObjectURL: (URL as unknown as { createObjectURL?: unknown }).createObjectURL,
  revokeObjectURL: (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL,
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  const u = URL as unknown as Record<string, unknown>
  for (const [name, orig] of Object.entries(ORIG_URL_METHODS)) {
    if (orig === undefined) delete u[name]
    else u[name] = orig
  }
  // ⭐ T5 review 的 Low-2（fix 轮）：本文件里 `render(<App/>)` 的那几条会**从盘上**读
  // 初始 tab/筛选（`uiState.readView`），而这里原先不清 localStorage ⇒ 下一条用例是
  // **从上一条留下的那个页面开跑**的（M6 #27「模块级状态跨用例串」的同族）。
  // ⚠️ 用常量（而不是字面量）是可以的：盘上这个键名只有 `uiState.ts` 一个真相，
  // 不与 `index.html` 的防闪脚本构成接口——必须用**字面量**的是
  // `ThemePicker.dom.test.tsx` 里那条断言（键名在两处，常量守不住漂移）。
  localStorage.removeItem(VIEW_STORAGE_KEY)
})

const AVAILABLE: GroupsResponse = {
  available: true,
  reason: '',
  generated_at: 1_700_000_999,
  groups: [
    { id: 111, name: '甲群', messages: 1664, last_ts: 1_700_000_000, watched: true, known: true },
  ],
}

const CATS: CategoriesResponse = {
  categories: [
    { slug: 'notice', label: '通知公告', icon: '📋', desc: 'x', count: 5 },
    { slug: 'academic', label: '学业', icon: '📚', desc: 'y', count: 0 },
  ],
  aliases: {},
}

/** 进入某一节（`Settings` 自己渲染在 `App` 之外时，「类目」这类名字不会与导航撞车）。 */
async function openSection(name: string) {
  await userEvent.click(screen.getByRole('button', { name }))
}

// ── 群 / 快照 ─────────────────────────────────────────────────────────

test('没有群快照时必须显示原因，而不是一张空表', async () => {
  /**
   * ⭐ 「一个群都没有」与「还没读过」是**两句不同的话**。
   * 空表会被读成前者——一句假话，而且是最容易让人不再信任这个页面的那种。
   *
   * ⚠️ 变异反证：让 `GroupsSection` 在 `!available` 时照样渲染
   * `<ul>`（拿到一个空 `groups` 就画空表）⇒ 本条**必红**（断言①）。
   * ⚠️ FM1b 是为此补的**探针**：保留原因横幅、另画一张空表 ⇒ 红在断言②
   * （`expected <ul></ul> to be null`）——**证明第②条断言有独立的牙**，
   * 不是被断言①捎带着守住的。两边都守，是因为界面**完全可能**忽略那个布尔字段。
   */
  installFetch({
    groups: {
      available: false,
      reason: '还没有群清单快照。请先在仓库根目录跑一次 `uv run vigil export`。',
      generated_at: null,
      groups: [],
    },
  })
  render(<Settings />)

  expect(await screen.findByText(/还没有群清单快照/)).toBeTruthy()
  // ⚠️ 第二条断言是「没画空表」的**机械**判据：不是"看着不像表"，
  // 而是页面上**根本没有列表元素**（`<ul>` 的 role 是 list）。
  expect(screen.queryByRole('list')).toBeNull()
})

test('群清单必须写出快照的生成时刻（快照会过期）', async () => {
  /**
   * ⭐ spec §4.2 ③ 的硬要求之一：快照是 CLI **预生成**的产物，会过期。
   * 不写时刻的话，用户会拿一份三天前的清单做决定。
   *
   * ⚠️ 变异反证：① 把「清单来自 … 的快照」那句整段删掉 ⇒ 红在 `findByText(/清单来自/)`；
   * ② 把 `* 1000` 拿掉（秒当毫秒）⇒ 红在第二条断言
   * （实测 `expected '清单来自 1970/1/21 …' to contain '2023/11/15 …'`）。
   */
  installFetch({ groups: AVAILABLE })
  render(<Settings />)

  const line = await screen.findByText(/清单来自/)
  expect(line.textContent).toContain(
    new Date(AVAILABLE.generated_at! * 1000).toLocaleString('zh-CN'),
  )
  expect(await screen.findByText('甲群')).toBeTruthy()
})

test('加群之后必须说清代价：下次 export 会拉全量历史', async () => {
  /**
   * ⭐ spec §4.2 ③ 的又一条硬要求：**不给数字的提示等于没提示**。
   * 首轮实测 1,664 条 / 21 批 / 6.3 分钟——不说的后果是用户加了个万人大群，
   * 然后以为程序卡死了。
   *
   * ⚠️ 变异反证：把 `submit()` 里那句 `setMsg(...)` 换成一句「已加入」
   * （或删掉）⇒ 本条**必红**。
   */
  const { calls } = installFetch({ groups: AVAILABLE })
  render(<Settings />)

  await screen.findByText('甲群')
  await userEvent.type(screen.getByPlaceholderText('群号'), '444')
  await userEvent.type(screen.getByPlaceholderText('群名'), '丁群')
  await userEvent.click(screen.getByRole('button', { name: '加入监视' }))

  const msg = await screen.findByText(/全部历史/)
  expect(msg.textContent).toContain('1,664')
  expect(msg.textContent).toContain('6.3 分钟')
  // ⚠️ 机制也要断言：提示写了却没有真的发出 POST，那是一句谎话。
  expect(calls.some((c) => c.method === 'POST' && c.url === '/api/groups')).toBe(true)
})

test('提交中的「加入监视」必须点不动（双击不许发两次 POST）', async () => {
  /**
   * ⭐ Fix loop 1 / m5：一次请求在飞的时候再点一下，第二次会撞 409
   * 「群已经在配置里了」——而那句红字会盖掉第一句成功提示，
   * 用户看到的是"我刚加的那个群报错了"。
   *
   * ⚠️ 变异反证：把按钮上的 `disabled={busy}` 或 `busy` 整个状态去掉
   * ⇒ 本条**必红**（`expected false to be true`）。
   *
   * ⚠️ 判据取「按钮此刻是不是禁用的」+「POST 只发了一次」两条：
   * 只有前者的话，一个"禁用了但 onClick 照跑"的实现照样绿
   * （React 里那是不可能的，但这条断言是**行为**层面的，不依赖那个内部知识）；
   * 只有后者的话，不点第二下就恒为 1（今天这条用例就是只点一下）。
   */
  const { calls, release } = installFetch({ groups: AVAILABLE, holdGroupPost: true })
  render(<Settings />)

  await screen.findByText('甲群')
  await userEvent.type(screen.getByPlaceholderText('群号'), '444')
  // ⚠️ 群名必须填（re-review Ⅱ-1 起手填表单要求非空）：不填的话下面那个 POST
  // **根本不会发**，而这批断言（"按钮禁用 + 只发一次"）会红在**与它们想守的
  // 东西无关**的原因上——一条把排障指向错方向的假红。
  await userEvent.type(screen.getByPlaceholderText('群名'), '丁群')
  await userEvent.click(screen.getByRole('button', { name: '加入监视' }))

  await waitFor(() => {
    const btn = screen.getByRole('button', { name: '加入监视' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  // 第二下：按钮已经禁用了，`userEvent` 对禁用元素不会派发 click
  // （真浏览器里也是这个行为）——所以下面数 POST 的条数才是"点不动"的证据。
  await userEvent.click(screen.getByRole('button', { name: '加入监视' }))
  expect(calls.filter((c) => c.method === 'POST' && c.url === '/api/groups').length).toBe(1)

  release()
  expect(await screen.findByText(/全部历史/)).toBeTruthy()
})

test('已停用的群（known 但没 watched）必须能一键启用，而不是给一个必然 409 的「加入监视」', async () => {
  /**
   * ⭐ **M6 终审 F2（Important）**：界面上「停用」从前是一条**单向门**。
   *
   * `/api/groups` 刻意把群分成两个字段：`watched` = 已纳入**且** `enabled`，
   * `known` = 已纳入（可能被停用）。而界面只读 `watched` ⇒ 一个 `enabled = false`
   * 的群走 `else` 支渲染成「加入监视」，那个 POST **必然** 409
   * 「群已经在配置里了」；而全文件里 `patchGroup` 只有 `{enabled:false}` 一处调用
   * ⇒ **网页上再也启用不回来**。而后端自己推荐的正确动作**正是**
   * 「改成 enabled = false」（`DELETE /api/groups/{gid}` 的 409 文案原文）。
   *
   * ⚠️ 四块断言，缺一块就是一个空壳：
   *   ① 停用的那个群渲染的是「启用」（不是「加入监视」）；
   *   ② 没纳入的那个群**照旧**渲染「加入监视」（`known:false` 那一支不许被牵连）；
   *   ③ 点「启用」发的是 **PATCH `/api/groups/222`**、体是 `{enabled:true}`；
   *   ④ **一个 POST /api/groups 都不许发**——那个 POST 是"必然 409"本身，
   *      只断言 ③ 的话，一个"两个都发"的实现照样绿。
   *
   * ⚠️ 变异反证（真跑过）：把 `g.known ? … : …` 那一支删回原来的两分支
   * ⇒ 本条**必红**（找不到「启用」）。
   */
  const { calls } = installFetch({
    groups: {
      available: true,
      reason: '',
      generated_at: 1_700_000_999,
      groups: [
        { id: 111, name: '甲群', messages: 1664, last_ts: 1_700_000_000, watched: true, known: true },
        { id: 222, name: '乙群', messages: 10, last_ts: 1_700_000_000, watched: false, known: true },
        { id: 333, name: '丙群', messages: 3, last_ts: 1_700_000_000, watched: false, known: false },
      ],
    },
  })
  render(<Settings />)

  await screen.findByText('甲群')
  // ① 停用的那个群
  const enable = await screen.findByRole('button', { name: '启用' })
  // ② 未纳入的那个群：列表里的「加入监视」只有一个（另一个同名按钮是底部手填表单）
  expect(screen.getAllByRole('button', { name: '加入监视' }).length).toBe(2)

  await userEvent.click(enable)
  // ③ PATCH 打到了**那个群**，体是启用
  const patch = calls.find((c) => c.method === 'PATCH')
  expect(patch?.url, '启用没有 PATCH 那个群').toBe('/api/groups/222')
  expect(patch?.body).toEqual({ enabled: true })
  // ④ 一个 POST 都没发
  expect(
    calls.filter((c) => c.method === 'POST' && c.url === '/api/groups').length,
    '给一个已在配置里的群发了「加入监视」——那个请求必然 409',
  ).toBe(0)
})

test('无名群（快照里 name 为空）的「加入监视」必须把空名原样发出去，不许编一个', async () => {
  /**
   * ⭐ **M6 终审 F2 的连带**：快照里「名字读不出来」是**正常状态**
   * （`groups_snapshot.collect` 刻意留 `""`，理由在它的 docstring 里），
   * 界面把它显示成「(无名) 111」。从前后端要求 name 非空 ⇒ 这个群
   * **必然**拿回 400「群名不能为空」，在网页上**加不进来**。
   *
   * ⚠️ 判据是**发出去的那个名字**：`{name: ''}` —— **不是**"随便补一个"。
   * 补一个（`群 111` / `(无名)`）是把一个**我们不知道的事实**写进配置文件，
   * 而它之后会出现在日报与信息流里，与真群名无从区分。
   *
   * ⚠️ 变异反证（真跑过）：把 `api_add_group` 改名后的空名检查加回去
   * ⇒ 后端那条用例红在 400；前端这条钉的是**请求体**（`name: ''`），
   * 把 `g.name` 换成 `g.name || '群' + g.id` ⇒ 本条**必红**。
   */
  const { calls } = installFetch({
    groups: {
      available: true,
      reason: '',
      generated_at: 1_700_000_999,
      groups: [
        { id: 444, name: '', messages: 7, last_ts: 1_700_000_000, watched: false, known: false },
      ],
    },
  })
  render(<Settings />)

  expect(await screen.findByText(/无名/)).toBeTruthy()
  // ⚠️ 列表里的那个在**前**（`<ul>` 在表单之前），表单那个同名按钮在**后**
  await userEvent.click(screen.getAllByRole('button', { name: '加入监视' })[0])

  const post = calls.find((c) => c.method === 'POST' && c.url === '/api/groups')
  expect(post?.body, '无名群发出去的名字被补成了一个我们自己编的值').toEqual({
    id: 444,
    name: '',
    tier: 'normal',
  })
})

test('手填表单不许把群名留空（这一页没有改名入口，空名群在网页上改不回来）', async () => {
  /**
   * ⭐ **re-review Ⅱ-1（Minor）**：`/api/groups` 从 F2 起**接受空名**（那是给
   * 「快照里名字取不到」的群留的路——上面那条用例钉着它），而这一页
   * **没有任何改名入口**（全文件 `patchGroup` 只有 `{enabled:true/false}` 两处）
   * ⇒ 手滑加的空名群在网页上永远改不回来，只能去手改 `config/groups.toml`。
   *
   * ⚠️ 判据是「**一个请求都不许发**」：客户端已经知道它是个坏值，发出去就等于
   * 造出那个改不回来的群（与人物节「QQ 号必须是正整数」同形）。
   *
   * ⚠️ 三条断言各自的变异形状（**顺序是刻意的**，见下）：
   *   ① `POST 条数 == 0` —— 形状「整段校验被删掉」：坏值被发出去 ⇒ 红在①；
   *   ② 「而且有话说」 —— 形状「校验留着但**不出声**」（`if (…) return` 里
   *      少了那句 `setMsg`）：请求确实没发（① 照绿），而用户点了按钮什么都
   *      没发生 ⇒ 红在②。**两半都要**：只守①的话，一个"静默拦下"的实现照样绿
   *      （那正是 M5 记过的「点了撤销、什么都没发生、也没有任何话说」）。
   *   ③ 补上名字之后**发得出去** —— 防"一刀切禁掉提交"：只有①②的话，
   *      一个把 `submit()` 开头直接 `return` 的实现照样绿。
   *
   * ⚠️ 顺序为什么是①在前（实测的）：把「有话说」放前面时，形状「删掉校验」
   * 会让**那句话先找不到**（`)` 之后它被成功提示覆盖/压根没设），
   * 报出来的红是②——而真正承重的那件事（坏值被 POST 出去了）没被说出来。
   * ⚠️ 代价如实记：在"校验整段删除"这个形状下②**到不了**（①先红、
   * 用例就停了）；它只在"静默拦下"那个形状里上场。
   *
   * ⚠️ 只收紧**这个表单**、不动服务端：上面那条「无名群必须把空名原样发出去」
   * 今天照绿，就是这件事的证据（无名群走的是列表里那个按钮，不是这个表单）。
   */
  const { calls } = installFetch({ groups: AVAILABLE })
  render(<Settings />)

  await screen.findByText('甲群')
  await userEvent.type(screen.getByPlaceholderText('群号'), '444')
  await userEvent.click(screen.getByRole('button', { name: '加入监视' }))

  expect(
    calls.filter((c) => c.method === 'POST' && c.url === '/api/groups').length,
    '客户端已经知道它是个坏值，却还是把请求发出去了',
  ).toBe(0)
  expect(await screen.findByText(/群名不能空着/)).toBeTruthy()

  await userEvent.type(screen.getByPlaceholderText('群名'), '丁群')
  await userEvent.click(screen.getByRole('button', { name: '加入监视' }))
  const post = calls.find((c) => c.method === 'POST' && c.url === '/api/groups')
  expect(post?.body, '补上群名之后仍然发不出去——那条校验把入口焊死了').toEqual({
    id: 444,
    name: '丁群',
    tier: 'normal',
  })
})

// ── 类目 ──────────────────────────────────────────────────────────────

test('删类目时未知条目数就必须要求选择迁移目标', async () => {
  /**
   * ⭐ 出口判据 6 的界面形态：「不允许静默留下指向不存在 slug 的条目」。
   *
   * ⚠️ 变异反证：把「确认删除」按钮的
   * `disabled={pending.count > 0 && !migrateTo}` 去掉 ⇒ 本条**必红**。
   *
   * ⚠️ 三条断言缺一不可（否则是一条空守卫）：
   *   ① 先说清「还有 N 条」——只说"要选目标"而不给 N，用户不知道这事多大；
   *   ② N > 0 时按钮**点不动**（这才是判据 6 的机械形式）；
   *   ③ **选了目标之后按钮要点得动**——只有 ② 的话，一个把按钮永久禁用的
   *      实现照样绿，而那等于这个类目永远删不掉。
   */
  installFetch({ categories: CATS })
  render(<Settings />)

  await openSection('类目')
  await screen.findByText(/通知公告/)
  await userEvent.click(screen.getAllByRole('button', { name: '删除' })[0])

  const prompt = await screen.findByText(/还有/)
  expect(prompt.textContent).toContain('5')
  const confirm = screen.getByRole('button', { name: '确认删除' }) as HTMLButtonElement
  expect(confirm.disabled).toBe(true)

  await userEvent.selectOptions(screen.getByRole('combobox'), 'academic')
  expect(
    (screen.getByRole('button', { name: '确认删除' }) as HTMLButtonElement).disabled,
  ).toBe(false)
})

test('撤销类目改动失败时必须有话说（未处理的 rejection 不算话）', async () => {
  /**
   * ⭐ Fix loop 1 / m1：原来只有 `.then()`，没有 `.catch()`。
   * 撤销失败（空栈 409、overlay 锁住 5xx）会变成一条**未处理的 rejection**：
   * 控制台一行红字，界面**一个字都没有**——用户点了撤销、什么都没发生。
   *
   * ⚠️ 变异反证：去掉 `.catch(...)` ⇒ 本条**必红**（那句话不会出现）。
   */
  const { calls } = installFetch({ categories: CATS, undoStatus: 409 })
  render(<Settings />)

  await openSection('类目')
  await screen.findByText(/通知公告/)
  await userEvent.click(screen.getByRole('button', { name: /撤销上一次类目改动/ }))

  expect(await screen.findByText(/没有可撤销的类目改动/)).toBeTruthy()
  expect(calls.some((c) => c.url.endsWith('/api/categories/undo'))).toBe(true)
})

test('撤销成功那句话的范围说明：别名映射跟着回滚，不在范围里的只有群 / 人物', async () => {
  /**
   * ⭐ **re-review Ⅰ-1（Important，= F1 那条判据的第二次现身）**。
   *
   * 从前这句写的是「群 / 人物 / **改名映射**不在这次回滚的范围里」——而别名表
   * **跟着一起回滚**：F1 的快照存的就是「`categories.toml` 原文 + 那一刻的
   * 别名表」这一对，撤销时整表换回去（reviewer 的探针：`导入前 {}` →
   * `导入后 {'life':'sport'}` → `撤销后 {}`）。三个名字里有一个是假的，
   * 而它是用户**唯一**读到的范围说明。
   *
   * ⚠️ 这条守的是**文案**，行为那一半由
   * `tests/test_api.py::test_api_import_then_undo_rolls_the_alias_table_back_too`
   * 守（"别名表真的回到导入前那一份"）。**两者缺一不可**：只修后端，这句话
   * 仍然在骗人；只改文案，下一个人改回去也没有任何东西会红。
   *
   * ⚠️ 变异反证：把「改名映射」放回「不在」那半句 ⇒ 本条**必红**
   * （第二条断言要的正是"它们不许出现在同一个句子里"）。
   */
  installFetch({ categories: CATS })
  render(<Settings />)

  await openSection('类目')
  await screen.findByText(/通知公告/)
  await userEvent.click(screen.getByRole('button', { name: /撤销上一次类目改动/ }))

  const msg = (await screen.findByText(/已撤销/)).textContent ?? ''
  expect(msg).toContain('改名映射')
  // 「改名映射」与「不在」不许出现在同一句话里（中间不跨那个括号）
  expect(msg).not.toMatch(/改名映射[^（]*不在/)
  // 群 / 人物**确实**不在范围里（这一半是真的，不许被顺手改掉）
  expect(msg).toMatch(/群\s*\/\s*人物[^。]*不在/)
})

// ── 人物 ──────────────────────────────────────────────────────────────

test('加人失败必须说出来（客户端拦下 & 服务端拒绝两个分支）', async () => {
  /**
   * ⭐ Fix loop 1：人物节的失败分支**零覆盖**——把 `setErr` 删掉，
   * 30 条前端用例全绿，而界面上是静默的。
   *
   * ⚠️ 变异反证：删掉 `add()` 的 `catch` 里的 `setErr(...)` ⇒ 本条**必红**。
   *
   * ⚠️ 两个分支要分清：客户端拦下时**一个请求都不该发**（0 不是有效账号，
   * 发出去只会拿回一个 400）；服务端拒绝时那句话必须**显示出来**。
   * 只测后者的话，"不发请求"这个更便宜的性质没人守。
   */
  const { calls } = installFetch({
    personPost: { status: 400, body: { detail: '服务端拒绝了：这个 uin 不在允许名单里' } },
  })
  render(<Settings />)

  await openSection('人物')
  const uinBox = await screen.findByPlaceholderText('QQ 号')
  await userEvent.type(uinBox, '0')
  await userEvent.type(screen.getByPlaceholderText('名字（辅导员张老师）'), '谁')
  await userEvent.click(screen.getByRole('button', { name: '新增' }))

  expect(await screen.findByText(/QQ 号必须是正整数/)).toBeTruthy()
  expect(
    calls.some((c) => c.method === 'POST' && c.url === '/api/persons'),
    '客户端已经知道它是个坏值，却还是把请求发出去了',
  ).toBe(false)

  await userEvent.clear(uinBox)
  await userEvent.type(uinBox, '123456')
  await userEvent.click(screen.getByRole('button', { name: '新增' }))
  expect(await screen.findByText(/不在允许名单里/)).toBeTruthy()
})

// ── 导入导出 ──────────────────────────────────────────────────────────

test('导入一个包：FileReader 读出来 → POST → notes / warnings 显示出来', async () => {
  /**
   * ⭐ Fix loop 1：`PackSection` 的导入半边**零覆盖**——
   * 把 `run()` / `readFile()` 整个删掉，30 条用例全绿，而这条路径是
   * 「配置的导入」唯一的人口。
   *
   * ⚠️ 变异反证：
   *   ① `readFile()` 改成 no-op ⇒ 红在 `findByText(/已读入 pack.toml/)`；
   *   ② `run()` 改成 no-op（或删掉两个按钮的 onClick）⇒ 红在 notes 那句。
   *
   * ⚠️ `notes` / `warnings` 必须**看得见**：T7 的 I4 专门修过这件事
   * （`on_progress` 曾经是个空 lambda）——后端把话带回来了，
   * 界面丢掉的话，用户只会看到一句"导入成功"，而他的 `qq_db_dir`
   * 已经被换成**别台机器**的路径了。
   *
   * ⚠️ 请求体也要断言（`apply` / `keep_settings`）：只断言"页面上有字"
   * 的话，一个永远发 `apply: false` 的实现能绿——而它一个字节都不写。
   */
  const { calls } = installFetch({
    packPost: {
      status: 200,
      body: {
        applied: true,
        groups: 15,
        categories: 7,
        persons: 2,
        aliases: 1,
        notes: ['本机那份 groups.toml 读不出来，已整份采用包里的 [settings]'],
        warnings: ['包里的 qq_db_dir 指向别台机器：C:/别人家的/nt_db'],
      },
    },
  })
  render(<Settings />)

  await openSection('导入导出')
  const file = new File(['schema = 1\n[settings]\n'], 'pack.toml', { type: 'text/plain' })
  await userEvent.upload(screen.getByLabelText('选择配置包文件'), file)

  // ⚠️ `FileReader` 是**异步**的 ⇒ 必须等界面上的那句话，不能紧接着断言
  expect(await screen.findByText(/已读入 pack.toml/)).toBeTruthy()
  expect(screen.getByDisplayValue(/schema = 1/)).toBeTruthy()

  await userEvent.click(screen.getByRole('button', { name: '导入并覆盖本机配置' }))

  expect(await screen.findByText(/已整份采用包里的 \[settings\]/)).toBeTruthy()
  expect(screen.getByText(/C:\/别人家的\/nt_db/)).toBeTruthy()
  expect(screen.getByText(/已应用/)).toBeTruthy()

  const post = calls.find((c) => c.method === 'POST' && c.url === '/api/config/pack')
  expect(post?.body).toEqual({
    toml: 'schema = 1\n[settings]\n',
    apply: true,
    keep_settings: true,
  })
})

test('「先校验不写入」必须发 apply=false，并且空包不许发出去', async () => {
  /**
   * ⭐ 预校验是**导入的安全带**：它必须真的走 `apply: false`
   * （后端那条路一个字节都不写），而不是"前端自己看看格式"。
   *
   * ⚠️ 变异反证：把 `run(false)` 改成 `run(true)`（或把
   * `disabled={busy || !importText.trim()}` 的第二个条件删掉）⇒ 本条**必红**。
   */
  const { calls } = installFetch({
    packPost: {
      status: 200,
      body: { applied: false, groups: 15, categories: 7, persons: 2, aliases: 1 },
    },
  })
  render(<Settings />)

  await openSection('导入导出')
  const check = screen.getByRole('button', { name: '先校验不写入' }) as HTMLButtonElement
  expect(check.disabled, '空包也让它发出去？那只会拿回一个 400').toBe(true)

  await userEvent.upload(
    screen.getByLabelText('选择配置包文件'),
    new File(['schema = 1\n'], 'pack.toml', { type: 'text/plain' }),
  )
  await screen.findByText(/已读入 pack.toml/)
  await userEvent.click(screen.getByRole('button', { name: '先校验不写入' }))

  expect(await screen.findByText(/校验通过/)).toBeTruthy()
  const post = calls.find((c) => c.method === 'POST' && c.url === '/api/config/pack')
  expect((post?.body as { apply: unknown }).apply).toBe(false)
})

test('导入失败（服务端 400）必须把原因说出来，而不是静默', async () => {
  /**
   * ⚠️ 变异反证：删掉 `run()` 的 `catch` 里的 `setErr(...)` ⇒ 本条**必红**。
   *
   * ⚠️ 400 与 500 是**两句不同的话**：400 是"这个包有问题"（用户能自己改），
   * 500 是"服务坏了"。这句判据落在**界面上显示的是哪一句**——
   * `api.ts` 的 `sendJSON` 已经把后端的 `detail` 抛上来了，别在这里丢掉它。
   */
  installFetch({
    packPost: {
      status: 400,
      body: { detail: '配置包读不出来：第 3 行 [[person]] 少写了一个 s（会静默读成 0 个人）' },
    },
  })
  render(<Settings />)

  await openSection('导入导出')
  await userEvent.upload(
    screen.getByLabelText('选择配置包文件'),
    new File(['[[person]]\n'], 'bad.toml', { type: 'text/plain' }),
  )
  await screen.findByText(/已读入 bad.toml/)
  await userEvent.click(screen.getByRole('button', { name: '导入并覆盖本机配置' }))

  expect(await screen.findByText(/少写了一个 s/)).toBeTruthy()
})

test('「下载 .toml」必须造出 Blob 链接、挂对文件名、用完 revoke', async () => {
  /**
   * ⭐ Fix loop 1：下载按钮**零覆盖**——把 `download()` 删掉，30 条用例全绿。
   *
   * ⚠️ 变异反证：`download()` 改成 no-op ⇒ 本条**必红**（`createObjectURL` 不会被调用）。
   *
   * ⚠️ 三件事都要断言，缺一条就是一个"看起来在守"的空壳：
   *   ① `createObjectURL` 收到的**是个 Blob 且里面装着 TOML**（不是空串）；
   *   ② `<a download>` 的**文件名**对；
   *   ③ 用完 `revokeObjectURL`——不撤的话每点一次都在页面里留一个句柄，
   *      而"泄漏了看不见"正是本项目最讨厌的形状。
   * ⚠️ jsdom **没有实现** `URL.createObjectURL`（直接调会抛），所以这里是
   * 挂上去一个替身而不是 mock 一个已有实现——挂之前它在不在都不影响。
   */
  // ⚠️ 形参必须**声明出来**（`_b: Blob`）：不声明的话 `mock.calls` 是空元组类型，
  // `calls[0][0]` 在 `tsc -b` 下直接报 TS2493/TS2352——而 `npm test` 照样绿。
  // 这正是「改前端必须跑 `npm run build`」那条纪律存在的理由。
  const createUrl = vi.fn((_b: Blob) => 'blob:http://localhost/fake-uuid')
  const revokeUrl = vi.fn((_u: string) => undefined)
  ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = createUrl
  ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = revokeUrl
  const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

  // ⚠️ 只抓 `<a>`：React 19 造每个 DOM 节点都走 `document.createElement`，
  // 于是这个 spy 会看到很多别的东西（Settings 里本来没有 <a>，所以 <a> 就是我们的）。
  const anchors: HTMLAnchorElement[] = []
  const origCreate = document.createElement.bind(document)
  vi.spyOn(document, 'createElement').mockImplementation(((
    tag: string,
    options?: ElementCreationOptions,
  ) => {
    const el = origCreate(tag, options)
    if (tag === 'a') anchors.push(el as HTMLAnchorElement)
    return el
  }) as typeof document.createElement)

  installFetch({})
  render(<Settings />)

  await openSection('导入导出')
  const btn = (await screen.findByRole('button', { name: '下载 .toml' })) as HTMLButtonElement
  await waitFor(() => expect(btn.disabled).toBe(false))
  await userEvent.click(btn)

  expect(createUrl).toHaveBeenCalledTimes(1)
  const blob = createUrl.mock.calls[0][0] as Blob
  expect(blob).toBeInstanceOf(Blob)
  expect(await blob.text()).toBe('# pack\n[[groups]]\nid = 1\n')

  expect(anchors.length).toBe(1)
  expect(anchors[0].download).toBe('vigil-config.toml')
  expect(anchors[0].href).toContain('blob:')
  expect(clickSpy).toHaveBeenCalledTimes(1)
  expect(revokeUrl).toHaveBeenCalledTimes(1)
})

test('上一条不许把替身留在全局（URL.createObjectURL 必须被还原）', () => {
  /**
   * ⭐ Fix loop 2 / Minor-1 的**泄漏探测器**。
   *
   * ⚠️ 判据是 `vi.isMockFunction(URL.createObjectURL) === false`
   * ——不用"它是不是 undefined"来判，因为那取决于 jsdom 版本有没有实现它；
   * **"它是不是一个 mock"才是"上一条用例有没有把它留在全局"的直接判据**。
   *
   * ⚠️ 这条**依赖执行顺序**（vitest 在同一文件内按声明序串行跑），
   * 而位置就是它的语义：**紧跟在唯一会挂替身的那条用例之后**。
   * 变异反证：把 `afterEach` 里那段还原删掉（= 回到"直接赋值、没人还"的旧写法）
   * ⇒ 本条**必红**（`expected true to be false`）。
   *
   * ⚠️⚠️ **它只在"整个文件一起跑"时才有牙**：用 `-t` 单跑它自己，
   * 上一条（唯一会挂替身的那条）**根本没执行** ⇒ 它绿——
   * **我第一遍的变异反证就是这么被骗过的**（一条"看起来守住了"的绿，
   * 而它守的东西压根没上过场：E7 形态 3「变异不在执行路径上」的同族）。
   * 全文件跑时那条红是实测的（见 T9 报告 Fix loop 2）。
   */
  expect(vi.isMockFunction(URL.createObjectURL)).toBe(false)
  expect(vi.isMockFunction(URL.revokeObjectURL)).toBe(false)
})

// ── 类目：新增 / 编辑（D1 那两块 UI）───────────────────────────────────

test('新增类目：填完提交必须调 POST /api/categories 并重取列表', async () => {
  /**
   * ⭐ Fix loop 2 / FIX 2：D1 补的那两块 UI（新增 / 编辑）**零覆盖**——
   * reviewer 实测：把 `onClick={doAdd}` 换成 `() => {}`，40 条用例全绿。
   * 上一轮只移掉了那颗**具体的雷**（D1 之前的那份备份），
   * **埋雷的条件还在**：没有任何用例碰过 `addCategory` / `updateCategory`。
   *
   * ⚠️ 变异反证：`onClick={doAdd}` → `onClick={() => {}}` ⇒ 本条**必红**。
   *
   * ⚠️ 三条断言各管一件事：
   *   ① 请求真的发出去了，且**请求体逐字**是那四个字段（字段名写错=静默丢字段）；
   *   ② 列表**重取**了（`提交 ⇒ 重取` 是四节共用的那个模式，M5 的两个缺陷同形）；
   *   ③ 界面说了话（不然用户不知道存没存上）。
   *   只有 ① 的话，一个"存了但界面不更新"的实现照样绿。
   */
  const { calls } = installFetch({ categories: CATS })
  render(<Settings />)

  await openSection('类目')
  await screen.findByText(/通知公告/)

  const add = screen.getByRole('button', { name: '新增' }) as HTMLButtonElement
  expect(add.disabled, '两个必填都空着，按钮该是点不动的').toBe(true)

  await userEvent.type(screen.getByPlaceholderText('中文名'), 'T9冒烟类目')
  await userEvent.type(screen.getByPlaceholderText('slug'), 't9smoke')
  await userEvent.type(
    screen.getByPlaceholderText('判据（会逐字进 LLM 提示词）'),
    '临时类目',
  )
  await userEvent.click(add)

  const post = calls.find((c) => c.method === 'POST' && c.url === '/api/categories')
  expect(post, '一次 POST /api/categories 都没发').toBeTruthy()
  expect(post?.body).toEqual({
    slug: 't9smoke',
    label: 'T9冒烟类目',
    desc: '临时类目',
    icon: '',
  })
  expect(
    calls.filter((c) => c.method === 'GET' && c.url === '/api/categories').length,
    '提交之后没有重取列表——用户看不到刚加的那个类目',
  ).toBeGreaterThan(1)
  expect(await screen.findByText(/已新增「T9冒烟类目」/)).toBeTruthy()
})

test('编辑类目：改判据提交必须调 PUT /api/categories/{slug}', async () => {
  /**
   * ⭐ Fix loop 2 / FIX 2 的另一半：`desc` 是**逐字进 LLM 提示词的判据**
   * （`types.ts` 的 `CategoryDetail` 写着「编辑器必须能改它」）,
   * 而这条路径此前一次都没被跑过。
   *
   * ⚠️ 变异反证：`onClick={() => doEdit(editing)}` → `onClick={() => {}}`
   * ⇒ 本条**必红**。
   *
   * ⚠️ 查询必须**限定在编辑框里**（`within`）：编辑框与下面的「新增类目」
   * 表单**共用同一批 placeholder**，不限定的话会撞"找到多个元素"
   * ——一条与被测行为毫无关系的红。
   */
  const { calls } = installFetch({ categories: CATS })
  render(<Settings />)

  await openSection('类目')
  await screen.findByText(/通知公告/)
  await userEvent.click(screen.getAllByRole('button', { name: '编辑' })[0])

  const box = screen.getByText(/编辑「notice」/).closest('div') as HTMLElement
  const desc = within(box).getByPlaceholderText('判据（会逐字进 LLM 提示词）')
  await userEvent.clear(desc)
  await userEvent.type(desc, '改过的判据')
  await userEvent.click(within(box).getByRole('button', { name: '保存' }))

  const put = calls.find((c) => c.method === 'PUT')
  expect(put, '一次 PUT 都没发').toBeTruthy()
  expect(put?.url).toBe('/api/categories/notice')
  expect(put?.body).toEqual({
    slug: 'notice',
    label: '通知公告',
    desc: '改过的判据',
    icon: '📋',
  })
  expect(await screen.findByText(/已保存「通知公告」/)).toBeTruthy()
})

// ── LLM ───────────────────────────────────────────────────────────────

test('保存 LLM 之后输入框显示的是服务端回的值（不是本地那串带空白的输入）', async () => {
  /**
   * ⭐ Fix loop 1 / m3 的行为那一半。
   *
   * ⚠️ 原来的注释说「显示的是**服务端回给的那个值**」，而实际只回填了
   * `thinking`——`base_url`/`model` 还是本地 state，**于是那句注释是半句假话**。
   * 这条用例钉的是「所见即所存」：`save()` 发出的是 `trim()` 过的值，
   * 后端存下、回给你的也是 trim 过的 ⇒ 输入框必须跟着变成**那个值**。
   *
   * ⚠️ 判据设计：输入的与回给的两者必须**不同**（这里输入 `  https://b/v1  `、
   * 服务端回 `https://b/v1`），否则这条用例区分不出"回填了"与"没回填"
   * （T3 I1/I2 的同一条判据）。
   *
   * ⚠️ 变异反证：去掉 `setBaseUrl(back.base_url)` ⇒ 本条**必红**
   * （输入框里留着 `  https://b/v1  `）。
   */
  const { calls } = installFetch({
    llm: { base_url: 'https://a/v1', model: 'm', enable_thinking: null },
    llmPut: { base_url: 'https://b/v1', model: 'm', enable_thinking: null },
  })
  render(<Settings />)

  await openSection('LLM')
  const base = (await screen.findByDisplayValue('https://a/v1')) as HTMLInputElement
  await userEvent.clear(base)
  await userEvent.type(base, '  https://b/v1  ')
  await userEvent.click(screen.getByRole('button', { name: '保存' }))

  await waitFor(() => {
    expect((screen.getByDisplayValue('https://b/v1') as HTMLInputElement).value).toBe(
      'https://b/v1',
    )
  })
  expect(
    (calls.find((c) => c.method === 'PUT' && c.url === '/api/llm')?.body as { base_url: string })
      .base_url,
    '发出去的必须是 trim 过的——界面上显示的那个值',
  ).toBe('https://b/v1')
})

// ── tab ───────────────────────────────────────────────────────────────

test('「配置」tab 在 App 里挂得上，且真的去读了群清单', async () => {
  /**
   * ⚠️ 变异反证：把 `App.tsx` 的 `{tab === 'settings' && <Settings />}`
   * 删掉 ⇒ 本条**必红**（点完「配置」页面上什么都没有）。
   */
  const { calls } = installFetch({ groups: AVAILABLE })
  render(<App />)

  await userEvent.click(await screen.findByRole('button', { name: '配置' }))
  expect(await screen.findByRole('button', { name: '导入导出' })).toBeTruthy()
  expect(calls.some((c) => c.url.startsWith('/api/groups'))).toBe(true)
})
