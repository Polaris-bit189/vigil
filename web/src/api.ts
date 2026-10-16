import type {
  CalendarPage,
  CategoriesResponse,
  Category,
  DigestDetail,
  DigestSummary,
  GroupsResponse,
  ItemDetail,
  ItemPage,
  JobRow,
  LlmSettings,
  Person,
} from './types'

import { noteFetchResult } from './connectivity.ts'

// ⚠️ `Person` 类型**定义在 `types.ts`**（Task 10 Step 4），这里只 import 再转出去。
// 两个文件各定义一份的话，TS 会以其中一份为准、另一份悄悄失效——
// 而 `types.ts` 是「与后端契约逐字对应」的那一份，它必须是唯一的真相源。
export type { Person }

/**
 * ⚠️ `noteFetchResult` 由 `connectivity.ts`（T3）提供。**这里先写死 import**——
 * T3 未落地时它会编译失败，所以 T2 与 T3 必须**同批**合入或按序（T3 先）。
 * 计划按 **T2 先、T3 紧随** 排；落地时若 T3 还没做完，把下面两处调用临时注释掉，
 * **但必须在 T3 里补回来**（否则状态机失去了"请求真的失败"这一路信号，
 * Review Focus #1 就没人守）。
 */
async function getJSON<T>(url: string): Promise<T> {
  let r: Response
  try {
    r = await fetch(url)
  } catch (e) {
    // fetch **本身抛** = 网络层没通（离线、DNS、连接被拒）。
    // ⚠️ 这与"服务器返回 500"是两回事：后者 fetch 不抛。
    noteFetchResult(false)
    throw e
  }
  // ⚠️ 只有 **5xx** 才算"后端不可达"：4xx 是"服务器活着、这个请求有问题"
  // （bad date、404），把它算成离线会让用户看到一个假的"离线"横幅。
  // ⚠️ 这正是"设置对了 ≠ 行为对了"（M4 二十五节）的落点：`navigator.onLine`
  // 是**设置**，"请求真的失败"是**行为**——两个都要看（spec §3.4）。
  noteFetchResult(r.status < 500)
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try {
      const body = (await r.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') msg = body.detail
    } catch {
      /* 响应不是 JSON（比如前端未构建时的 503 HTML），就用状态码 */
    }
    throw new Error(msg)
  }
  return (await r.json()) as T
}

async function sendJSON<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try {
      const b = (await r.json()) as { detail?: unknown }
      if (typeof b.detail === 'string') msg = b.detail
    } catch {
      /* 非 JSON 响应（比如未构建时的 503 HTML）就用状态码 */
    }
    throw new Error(msg)
  }
  // 204 没有 body——`r.json()` 会抛，必须显式短路
  if (r.status === 204) return undefined as T
  return (await r.json()) as T
}

export type ItemQuery = {
  /** ⚠️ 多值。**空数组与不传同义**（= 不筛选），别把 [] 拼成空参数。 */
  kinds?: string[]
  /** 人物维度，值是 uin（QQ 号）。 */
  persons?: number[]
  since?: string
  until?: string
  group?: number
  q?: string
  limit?: number
  offset?: number
}

export function fetchItems(query: ItemQuery): Promise<ItemPage> {
  const p = new URLSearchParams()
  // ⚠️ 必须用 append 而不是 set：set 会让后一个值覆盖前一个，
  // 于是"选了三个类目"发出去变成"只筛最后一个"，而**页面不会报错**。
  for (const k of query.kinds ?? []) {
    if (k) p.append('kind', k)
  }
  for (const u of query.persons ?? []) {
    p.append('person', String(u))
  }
  if (query.since) p.set('since', query.since)
  if (query.until) p.set('until', query.until)
  if (query.group !== undefined && query.group !== null) {
    p.set('group', String(query.group))
  }
  if (query.q) p.set('q', query.q)
  if (query.limit !== undefined) p.set('limit', String(query.limit))
  if (query.offset !== undefined) p.set('offset', String(query.offset))
  return getJSON<ItemPage>(`/api/items?${p.toString()}`)
}

/**
 * 日历数据：**只**含带截止日的条目，默认全量（第三版 spec §3.1）。
 *
 * ⚠️ 它**不走** `/api/items`：后者的 `limit` 上限是 200，而日历要全量。
 */
export const fetchCalendar = (range?: { since?: string; until?: string }) => {
  const p = new URLSearchParams()
  if (range?.since) p.set('since', range.since)
  if (range?.until) p.set('until', range.until)
  const qs = p.toString()
  return getJSON<CalendarPage>(`/api/calendar${qs ? `?${qs}` : ''}`)
}

export const fetchItem = (id: number) => getJSON<ItemDetail>(`/api/items/${id}`)

export const fetchCategories = () =>
  getJSON<{ categories: Category[] }>('/api/categories')

export const fetchDigests = () =>
  getJSON<{ digests: DigestSummary[] }>('/api/digests')

export const fetchDigest = (id: number) =>
  getJSON<DigestDetail>(`/api/digests/${id}`)

export const fetchPersons = () =>
  getJSON<{ persons: Person[] }>('/api/persons')

export const addPerson = (uin: number, label: string, note = '') =>
  sendJSON<{ uin: number; label: string }>('/api/persons', 'POST', {
    uin,
    label,
    note,
  })

export const removePerson = (uin: number) =>
  sendJSON<void>(`/api/persons/${uin}`, 'DELETE')

export const setItemKind = (itemId: number, kind: string) =>
  sendJSON<{ edit_id: number }>(`/api/items/${itemId}/kind`, 'POST', { kind })

export const deleteItem = (itemId: number) =>
  sendJSON<{ edit_id: number }>(`/api/items/${itemId}`, 'DELETE')

export const undoLast = (editId?: number) =>
  sendJSON<{ undone: boolean }>('/api/undo', 'POST', {
    edit_id: editId ?? null,
  })

// ── M6 T9：配置界面用的端点 ───────────────────────────────────────────
//
// ⚠️ 群那一组走的是**快照**（T4）：`/api/groups` 回的是 CLI 预生成的
// `data/groups-snapshot.json`，不是实时查 QQ 库——所以它有 `available`
// 这个字段，界面必须把「还没读过」与「一个群都没有」分开显示。

export const fetchGroups = () => getJSON<GroupsResponse>('/api/groups')

export const addGroup = (id: number, name: string, tier: string) =>
  sendJSON<{ id: number }>('/api/groups', 'POST', { id, name, tier })

export const patchGroup = (
  id: number,
  patch: { name?: string; enabled?: boolean; tier?: string },
) => sendJSON<{ id: number }>(`/api/groups/${id}`, 'PATCH', patch)

export const removeGroup = (id: number) => sendJSON<void>(`/api/groups/${id}`, 'DELETE')

export const fetchCategoriesDetailed = () =>
  getJSON<CategoriesResponse>('/api/categories')

export const addCategory = (slug: string, label: string, desc: string, icon: string) =>
  sendJSON<{ slug: string }>('/api/categories', 'POST', { slug, label, desc, icon })

export const updateCategory = (
  slug: string,
  body: { slug?: string; label?: string; desc?: string; icon?: string },
// ⚠️ slug 必须 `encodeURIComponent`——与 `removeCategory` **同一条待遇**（Fix loop 1 / m2）。
// 今天 slug 受后端的 `_SLUG_RE` 约束、里面不会出现需要转义的字符，所以这不是在修一个
// 眼前的 bug：**同文件里两处同形的 URL 拼接，一处编码一处不编码**，下一个人照着哪一处
// 写都说得通——而照着错的那一处写，只会在某个含 `/` 或空格的取值上才现形。
) =>
  sendJSON<{ slug: string }>(
    `/api/categories/${encodeURIComponent(slug)}`,
    'PUT',
    body,
  )

export const removeCategory = (slug: string, migrateTo?: string) =>
  sendJSON<void>(
    `/api/categories/${encodeURIComponent(slug)}` +
      (migrateTo ? `?migrate_to=${encodeURIComponent(migrateTo)}` : ''),
    'DELETE',
  )

export const undoCategorieChange = () =>
  sendJSON<{ undone: boolean }>('/api/categories/undo', 'POST')

export const fetchLlmSettings = () => getJSON<LlmSettings>('/api/llm')

export const saveLlmSettings = (s: LlmSettings) =>
  sendJSON<LlmSettings>('/api/llm', 'PUT', s)

export const startReorder = () =>
  sendJSON<JobRow>('/api/reorder', 'POST')

export const fetchJob = (jobId: string) => getJSON<JobRow>(`/api/jobs/${jobId}`)

export const undoJob = (jobId: string) =>
  sendJSON<{ undone: boolean; count: number }>(`/api/jobs/${jobId}/undo`, 'POST')

// ── M6 T11：重排界面用到的三个端点 ───────────────────────────────────
//
// ⚠️ 它们此前在 `web/` 里**零引用**（`/api/jobs/{id}/cancel`、`/api/items/kinds`、
// `/api/bulk-undo`）：后端做了、测了、绿了，而**用户够不到**——那个特性事实上不存在。

/**
 * 请求中断一个重排。
 *
 * ⚠️ 返回体**不是 `JobRow`**——它是 `{"cancelled": bool, "state": str}`，
 * 与 `fetchJob` 那个端点**不同形**。（计划里这一行写的是 `sendJSON<JobRow>`，
 * 那会让调用方以为拿到的是完整 job：`job.state` 会是 `undefined`，
 * 而在 TS 上**一个字都不报**——类型说了一句假话。）
 *
 * ⚠️ `cancelled: true` 只说「请求已受理」：取消是**协作式**的（Python 没法从
 * 外面杀线程），正在飞的那一批会跑完，真正结束看轮询到的 `state == "cancelled"`。
 * 对**已经结束**的 job 它回 200 + `cancelled: false`，不是 409——那是"手慢了一步"。
 */
export const cancelJob = (jobId: string) =>
  sendJSON<{ cancelled: boolean; state: JobRow['state'] }>(
    `/api/jobs/${jobId}/cancel`,
    'POST',
  )

/** 人工多选批量改类目（D16 的「界面上多选批量改」那一半）。
 *  ⚠️ 返回的 `actor` 是**撤销的键**——丢了它就再也撤不回这一批。 */
export const bulkSetKind = (itemIds: number[], kind: string) =>
  sendJSON<{ actor: string; count: number }>('/api/items/kinds', 'POST', {
    item_ids: itemIds,
    kind,
  })

export const bulkUndo = (actor: string) =>
  sendJSON<{ undone: boolean; count: number }>('/api/bulk-undo', 'POST', { actor })

export const exportPack = () => getJSON<{ toml: string }>('/api/config/pack')

export const importPack = (toml: string, apply: boolean, keepSettings: boolean) =>
  sendJSON<Record<string, unknown>>('/api/config/pack', 'POST', {
    toml,
    apply,
    keep_settings: keepSettings,
  })
