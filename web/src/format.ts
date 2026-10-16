// 时间格式化。**一律按本地时区**——与 M2 日报的「本地日」口径一致
// （M2 实测：用 UTC 解释同一个日期串会差 8 小时，报出的数就对不上）。

const pad = (n: number) => String(n).padStart(2, '0')

export function fmtDate(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

export function fmtDateTime(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 只要时分（`14:00`）。**只用于「开始时刻」**。
 *
 * ⚠️ 存在的理由（用户 2026-10-15 裁定「只显示开始时间排表」）：
 * 活动的**结束时刻是推算的**（抽取没给时长，我们按 90 分钟假设），
 * 渲染成 `14:00–15:30` 会让用户以为是已知事实。
 * 开始时刻是**真的**（来自源文、过了 `start_supported` 机器闸门），
 * 所以只报它。
 */
export function fmtTime(ts: number): string {
  const d = new Date(ts * 1000)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function relTime(ts: number): string {
  const diff = Date.now() / 1000 - ts
  if (diff < 0) return fmtDate(ts) // 未来时间（脏数据）不编「-3 分钟前」
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  return fmtDate(ts)
}

/** 距死线的分级。与后端 `vigil/deadline.py::deadline_level` **必须同阈值**。 */
export type DeadlineLevel = 'expired' | 'urgent' | 'soon' | 'normal'

export interface DeadlineRel {
  text: string
  level: DeadlineLevel
}

/**
 * 距死线还有多久。**与 `relTime` 方向相反** —— 它算的是未来，
 * 而 `relTime` 的重心是过去（未来会直接回落到日期）。
 *
 * ⚠️ 死线**永远在未来**，所以「今天截止」这类文案必须由这个函数给；
 * 用 `relTime` 只会得到一个绝对日期（那是它的设计，不是 bug）。
 *
 * ⚠️ **分级规则在 TS 与 Python 各有一份，这是知情取舍**：
 *   · 日报要落盘成 `.md`（后端渲染），前端拿不到 ⇒ 后端必须有一份
 *   · 日历要跟随「当前时刻」变化（前端渲染），后端算完会过期 ⇒ 前端必须有一份
 * 两份注释互相指路，且测试各钉同一条边界（0 / 1 / 2 / 7 / 8 天）。
 *
 * ⚠️ 按**本地日**对齐（不是按秒差）：「明天 09:00 截止」在今晚 23:00
 * 应当是「明天截止」，而不是「0 天」——按 `diff/86400` 取整会算成 0。
 */
export function relDeadline(deadlineTs: number, nowTs?: number): DeadlineRel {
  const now = nowTs ?? Date.now() / 1000
  const days = dayDiff(deadlineTs, now)

  if (days < 0) return { text: `已过期 ${-days} 天`, level: 'expired' }
  if (days === 0) return { text: '今天截止', level: 'urgent' }
  if (days === 1) return { text: '明天截止', level: 'urgent' }
  if (days <= 3) return { text: `还有 ${days} 天`, level: 'soon' }
  if (days <= 7) return { text: `还有 ${days} 天`, level: 'normal' }
  // 超过一周：绝对日期比「还有 12 天」更好读——那时人已经在看日历了
  return { text: fmtDate(deadlineTs), level: 'normal' }
}

/** 两个时间戳相差几个**本地日**（正数 = 前者在未来）。忽略当日已过的时分秒。 */
function dayDiff(a: number, b: number): number {
  return Math.round((startOfLocalDay(a) - startOfLocalDay(b)) / 86400)
}

function startOfLocalDay(ts: number): number {
  const d = new Date(ts * 1000)
  d.setHours(0, 0, 0, 0)
  return d.getTime() / 1000
}
