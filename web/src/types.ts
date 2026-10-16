// API 契约的 TypeScript 影子。
//
// ⚠️ 这个文件是**冻结契约**的前端一侧：字段名与后端 `vigil/api.py` 的
// 输出逐字对应，改任何一边都必须同时改另一边（波级窄审查专门核这件事）。
// 类型写错不会在构建期报错——它只会让页面静默少一栏，所以字段名不许凭记忆改。

export type Item = {
  item_id: number
  kind: string // slug，如 "academic"
  kind_label: string // 中文，如 "学业"
  kind_icon: string // emoji
  title: string
  detail: string | null
  event_ts: number // Unix 秒
  /** ⚠️ 已过数据层核验：NULL 表示源文里找不到依据，前端**不显示**它 */
  deadline_ts: number | null
  group_id: number
  group_name: string
  actor: string | null
  /** QQ 号（uin）。实名消息才有；匿名消息为 null，且不该被人物筛选中。 */
  actor_uin: number | null
  place: string | null
  amount: string | null
  links: string[]
  source_count: number
  /**
   * 活动**开始时刻**（M7）。⚠️ 与 `event_ts` 是两件事：
   * `event_ts` = 消息发出的时间（永远有）；`start_ts` = 活动本身开始的时间。
   * **为 null 是常态**（多数条目没有活动时刻），不是异常。
   */
  start_ts: number | null
}

export type Source = {
  msg_id: number
  ts: number
  sender: string
  group_name: string
  content: string
}

export type ItemDetail = Item & { sources: Source[] }

export type ItemPage = {
  items: Item[]
  total: number
  limit: number
  offset: number
}

/**
 * 日历端点的响应。**与 `/api/items` 同形**（items 是同一套 `item_out`），
 * 只是**不分页**、只含带截止日的条目。
 */
/** 冲突里的一侧。`item_id` 为 null = 它不是条目（比如**你的课**）。 */
export type ConflictSide = {
  label: string
  start: number
  end: number
  item_id: number | null
  kind: 'activity' | 'course' | 'task'
}

export type Conflict = {
  a: ConflictSide
  b: ConflictSide
  /** 重叠多少分钟。给人看时要一个具体数。 */
  minutes: number
}

export type CalendarPage = {
  items: Item[]
  total: number
  /**
   * 「同一件事的重复」映射：`被折叠的 item_id → 代表它的那条 item_id`。
   *
   * ⚠️ 被折叠的条目**仍在 `items` 里**（行没被删，可回溯那条不变量要求如此）
   * —— 前端据此把它们**收起来**，而不是不渲染。见 `conflicts.py` ①。
   */
  duplicates: Record<string, number>
  /** 活动之间撞车（要用户选一个）。 */
  conflicts: Conflict[]
  /**
   * 与你的课冲突。
   * ⚠️ 课表读不出来时是 `[{ error: "…" }]` —— 与"课表是空的、没有冲突"
   * 在界面上必须**能分辨**，所以不是空数组。
   */
  course_conflicts: (Conflict | { error: string })[]
}

export type Category = {
  slug: string
  label: string
  icon: string
  count: number
}

export type DigestSummary = {
  digest_id: number
  day: string // YYYY-MM-DD
  window_from: number
  window_to: number
  created_at: number
  item_count: number
}

export type DigestDetail = DigestSummary & {
  body_md: string
  items: Item[]
}

/**
 * 一个被监视的人物。
 *
 * ⚠️ `count` 是"这个 uin 发过的条目数"，由后端算好——前端不要在拿到
 * 全部条目后自己数：那需要把整个库拉下来，而且计数会随筛选条件漂移。
 */
export type Person = {
  uin: number
  label: string
  note: string
  count: number
}

/** 一个 QQ 本地库里的群（**来自 CLI 预生成的快照**，不是实时查询）。 */
export type GroupRow = {
  id: number
  name: string
  /** 该群在 QQ 库里的**全量**历史消息条数（不是已导出的）。 */
  messages: number
  last_ts: number
  /** 已纳入且 **enabled**。 */
  watched: boolean
  /** 已纳入但可能被停用。 */
  known: boolean
}

export type GroupsResponse = {
  /**
   * ⚠️ `false` = **还没有快照**，此时 `reason` 一定非空、`groups` 一定是空数组。
   * 界面**必须**把这两件事分开显示：「一个群都没有」与「还没读过」是两句不同的话，
   * 混起来说的后果是用户以为自己的 QQ 库是空的。
   */
  available: boolean
  reason: string
  generated_at: number | null
  groups: GroupRow[]
}

export type CategoryDetail = Category & {
  /** 类目判据，会**逐字**进提示词。编辑器必须能改它。 */
  desc: string
}

export type CategoriesResponse = {
  categories: CategoryDetail[]
  /** `{旧 slug: 新 slug}` —— 改过名的类目在这里留痕。 */
  aliases: Record<string, string>
}

export type LlmSettings = {
  base_url: string
  model: string
  /** `null` = **不发这个字段**（通用 OpenAI 端点最安全的形状）。 */
  enable_thinking: boolean | null
}

export type JobRow = {
  job_id: string
  kind: string
  state: 'running' | 'done' | 'failed' | 'cancelled'
  total: number
  done: number
  message: string
  error: string
  actor: string
  started_at: number
  finished_at: number | null
}
