import { describe, expect, it, vi } from 'vitest'
import {
  addPerson,
  deleteItem,
  fetchItem,
  fetchItems,
  removeCategory,
  removePerson,
  setItemKind,
  undoLast,
  updateCategory,
} from '../api'
import { _resetForTests, isOnline, noteFetchResult } from '../connectivity.ts'

describe('fetchItems 的查询串', () => {
  it('丢掉 undefined / 空串，不发出空参数', async () => {
    const spy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
    })
    vi.stubGlobal('fetch', spy)
    // ⚠️ Task 10 改了 `ItemQuery`：`kind: string` → `kinds: string[]`。
    // 空串那条覆盖**保留**（数组里混进空 slug 时不许发出 `kind=`），
    // 只是换了载体——原来验的是"空串字段"，现在验的是"数组里的空串"。
    await fetchItems({ kinds: [''], q: undefined, limit: 50 })
    const url = String(spy.mock.calls[0][0])
    expect(url).toBe('/api/items?limit=50')
  })

  it('参数照原样带上', async () => {
    const spy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 20, offset: 40 }),
    })
    vi.stubGlobal('fetch', spy)
    await fetchItems({ q: '选课', limit: 20, offset: 40 })
    expect(String(spy.mock.calls[0][0])).toBe(
      '/api/items?q=%E9%80%89%E8%AF%BE&limit=20&offset=40',
    )
  })
})

describe('错误处理', () => {
  it('把后端的 detail 当错误消息抛出来', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: '没有这条条目：999' }),
    }))
    await expect(fetchItem(999)).rejects.toThrow('没有这条条目：999')
  })

  it('响应体不是 JSON 时回落到状态码，不吞错', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => { throw new Error('not json') },
    }))
    await expect(fetchItem(1)).rejects.toThrow('HTTP 503')
  })
})

// ── Task 10：多值查询串 ──────────────────────────────────────────

it('多值 kind 用重复参数而不是逗号拼接', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
  })
  vi.stubGlobal('fetch', spy)

  await fetchItems({ kinds: ['notice', 'academic'], limit: 50 })
  expect(spy.mock.calls[0][0]).toBe(
    '/api/items?kind=notice&kind=academic&limit=50',
  )
})

it('多值 person 同理', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
  })
  vi.stubGlobal('fetch', spy)

  await fetchItems({ persons: [111, 222], limit: 50 })
  expect(spy.mock.calls[0][0]).toBe('/api/items?person=111&person=222&limit=50')
})

it('空数组等于不筛选', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
  })
  vi.stubGlobal('fetch', spy)

  await fetchItems({ kinds: [], persons: [], limit: 50 })
  expect(spy.mock.calls[0][0]).toBe('/api/items?limit=50')
})

it('两个维度同时在场（维度内并集 × 维度间交集）', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
  })
  vi.stubGlobal('fetch', spy)

  await fetchItems({ kinds: ['notice', 'academic'], persons: [7], limit: 50 })
  // 后端把同一维度的重复参数收成一个 IN（并集）、两个维度之间是 AND。
  // 前端这一侧的责任只是"别在拼串时把两个维度揉在一起"。
  expect(spy.mock.calls[0][0]).toBe(
    '/api/items?kind=notice&kind=academic&person=7&limit=50',
  )
})

// ── Task 10：写端点的封装（Step 3 新增的 sendJSON）─────────────────
//
// ⚠️ 这几条**不是** brief 要求的，是执行期补的：`sendJSON` 里有两处
// 会静默出错的形状——① 用 `set` 而不是 `append`；② 204 没有 body 却被
// `r.json()` 解。它们错了以后**页面只会报一句看不懂的错**，而 M5 出口
// 判据 6 的「删条目 / 撤销」两条全靠这几个封装。

it('改分类：POST 到 /api/items/{id}/kind，body 带 kind', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ edit_id: 3 }),
  })
  vi.stubGlobal('fetch', spy)

  await expect(setItemKind(42, 'academic')).resolves.toEqual({ edit_id: 3 })
  const [url, init] = spy.mock.calls[0]
  expect(url).toBe('/api/items/42/kind')
  expect(init.method).toBe('POST')
  expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
  expect(init.body).toBe(JSON.stringify({ kind: 'academic' }))
})

it('删除：DELETE 不带 body、不带 Content-Type', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ edit_id: 9 }),
  })
  vi.stubGlobal('fetch', spy)

  await expect(deleteItem(42)).resolves.toEqual({ edit_id: 9 })
  const [url, init] = spy.mock.calls[0]
  expect(url).toBe('/api/items/42')
  expect(init.method).toBe('DELETE')
  // body 为 undefined 时**不许**发 Content-Type：一个声明了 JSON 却没有
  // body 的请求，Starlette 会在读 body 时给一个与真实原因无关的错。
  expect(init.headers).toBeUndefined()
  expect(init.body).toBeUndefined()
})

it('撤销：edit_id 缺省时显式发 null（后端「撤最近一步」的判据是 None）', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ undone: true }),
  })
  vi.stubGlobal('fetch', spy)

  await expect(undoLast()).resolves.toEqual({ undone: true })
  const [url, init] = spy.mock.calls[0]
  expect(url).toBe('/api/undo')
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body)).toEqual({ edit_id: null })
})

it('撤销：给了 edit_id 就撤那一条', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ undone: true }),
  })
  vi.stubGlobal('fetch', spy)

  await undoLast(7)
  expect(JSON.parse(spy.mock.calls[0][1].body)).toEqual({ edit_id: 7 })
})

it('删人物：204 没有 body，不许去解 JSON', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 204,
    // 照实模拟 204：真响应就是空 body，解它必然抛
    json: async () => {
      throw new Error('Unexpected end of JSON input')
    },
  })
  vi.stubGlobal('fetch', spy)

  await expect(removePerson(111)).resolves.toBeUndefined()
  expect(spy.mock.calls[0][0]).toBe('/api/persons/111')
  expect(spy.mock.calls[0][1].method).toBe('DELETE')
})

it('新增人物：201 有 body，按 JSON 解出来', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 201,
    json: async () => ({ uin: 111, label: '张老师' }),
  })
  vi.stubGlobal('fetch', spy)

  await expect(addPerson(111, '张老师')).resolves.toEqual({
    uin: 111,
    label: '张老师',
  })
  expect(JSON.parse(spy.mock.calls[0][1].body)).toEqual({
    uin: 111,
    label: '张老师',
    note: '',
  })
})

it('写端点失败时把后端的 detail 当错误消息抛出来', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: false,
    status: 404,
    json: async () => ({ detail: '没有这条条目：999' }),
  }))
  await expect(deleteItem(999)).rejects.toThrow('没有这条条目：999')
})

// ── T9 fix loop 1 / m2：类目那两处 slug 拼接必须**同一条待遇** ──────────
//
// ⚠️ 这两条不是在修一个眼前的 bug：今天 slug 受后端 `_SLUG_RE` 约束、
// 不会出现需要转义的字符。它们钉的是**形状**——`removeCategory` 一直编码、
// `updateCategory` 不编码，同一文件里两处同形的东西待遇相反，
// 下一个人照着哪一处写都说得通；照着错的那一处写，只会在某个
// 含 `/` 或空格的取值上才现形（而那时它是**一条 404**，不是一句配置错误）。
// ⇒ 两条成对，就是为了让"只有一处被改回去"也能被抓到。

it('改类目：slug 必须 encodeURIComponent', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ slug: 'a/b' }),
  })
  vi.stubGlobal('fetch', spy)

  await updateCategory('a/b', { label: 'x' })
  expect(spy.mock.calls[0][0]).toBe('/api/categories/a%2Fb')
  expect(spy.mock.calls[0][1].method).toBe('PUT')
})

it('删类目：slug 同理（与上一条逐字对照）', async () => {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 204,
    json: async () => {
      throw new Error('204 没有 body')
    },
  })
  vi.stubGlobal('fetch', spy)

  await removeCategory('a/b', 'life')
  expect(spy.mock.calls[0][0]).toBe('/api/categories/a%2Fb?migrate_to=life')
})

// ── T2 加固（review）：`getJSON` 的**分类**——它怎么调 `noteFetchResult` ──
//
// ⚠️ 为什么非补不可：`connectivity.test.ts` 测的是 `noteFetchResult` **这个函数
// 本身**（给它 false 就变离线），而**没有任何测试过 `getJSON` 是怎么调它的**。
// 于是 `getJSON` 里那句分类判断可以整句写错（或干脆不调）而测试全绿——
// 那正是 M4「设置对了 ≠ 行为对了」的反面：状态机是**行为**，这一组测试是唯一
// 把"设置"（HTTP 状态码 / fetch 是否抛）接到"行为"（isOnline）的关卡。
//
// 三档分类（与 `api.ts` 里 getJSON 的注释逐条对应）：
//   5xx ⇒ 后端不可达 ⇒ 离线；4xx ⇒ 服务器活着 ⇒ **保持在线**；fetch 抛 ⇒ 网络层没通 ⇒ 离线。
//
// ⚠️ 每条都要先 `_resetForTests()`：`connectivity` 的 `online` 是**模块级状态**，
// 不重置就会把上一档的结论带进下一档（"5xx 变离线"之后，"4xx 保持在线"其实在
// 测"它还是一直离线"，三档就互相串味了）。

describe('getJSON 把请求结局分类上报给 connectivity', () => {
  it('5xx ⇒ 抛错 + 离线（后端活着但不可用：502/503）', async () => {
    _resetForTests()
    expect(isOnline()).toBe(true) // 先证实起点在线，否则"变 false"无从谈起
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: '内部错误' }),
    }))
    await expect(fetchItems({ limit: 1 })).rejects.toThrow()
    expect(isOnline()).toBe(false)
    vi.unstubAllGlobals()
  })

  it('400/404 ⇒ 抛错但**回到在线**（4xx = 服务器活着，这不是离线）', async () => {
    // ⚠️ 必须**先落到离线**再断言它翻回在线。否则"4xx 上报 true"与
    // "4xx 干脆不上报"在起点已在线时**不可区分**——这是本仓 M4「设置对了≠
    // 行为对了」的同一形状，我在加固时自己踩过一次：初版这条起点是
    // `_resetForTests()` 后的在线态，于是把 getJSON 改成"4xx 不报"仍全绿。
    // 落到离线后，"不上报"会让断言红，"上报 true"才会绿。
    _resetForTests()
    noteFetchResult(false) // 造一个真实的离线态（比如刚才一次 5xx / 断网）
    expect(isOnline()).toBe(false)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: '没有这条条目：999' }),
    }))
    await expect(fetchItem(999)).rejects.toThrow('没有这条条目：999')
    // ⚠️ 这条是"假离线横幅"与"假在线"的守门员：把 4xx 也算成离线，用户会在
    // 一次 bad date / 404 之后看到莫名其妙的"离线"横幅；而不上报 4xx，
    // 用户会在后端其实已经恢复后一直卡在离线态。
    expect(isOnline()).toBe(true)
    vi.unstubAllGlobals()
  })

  it('fetch 本身抛 ⇒ 抛错 + 离线（网络层就没通：离线 / DNS / 连接被拒）', async () => {
    _resetForTests()
    expect(isOnline()).toBe(true)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(fetchItems({ limit: 1 })).rejects.toThrow()
    expect(isOnline()).toBe(false)
    vi.unstubAllGlobals()
  })
})
