/**
 * 视图状态（当前 tab + 类目/人物筛选）的持久化。
 *
 * 修掉 spec §1.5 点名的「刷新后 tab 与 kind 全部重置」。
 * ⚠️ 与主题分**两个 key**（`vigil.view` / `vigil.theme`）：清一个不该带上另一个。
 */
export type Tab = 'feed' | 'cats' | 'digests' | 'settings' | 'calendar'

export const TABS: { key: Tab; label: string }[] = [
  { key: 'feed', label: '信息流' },
  { key: 'cats', label: '类目' },
  { key: 'digests', label: '日报' },
  { key: 'settings', label: '配置' },
  // ⚠️ 日历**追加在末尾**（用户 2026-10-10 裁定「日历落第 5 个 tab」、spec §3.2
  // 「「日历」tab（第 5 个）」）——**不是**插在「配置」前当第 4 个。追加还避免
  // 改动 `argusLabel` 那套 `[n]` 序号（`argusBrand.dom.test.tsx` 钉死了
  // `[1]信息流…[4]配置`；插在中间会把它们全推后一位）。
  { key: 'calendar', label: '日历' },
]

export type View = { tab: Tab; kinds: string[]; persons: number[] }

export const VIEW_STORAGE_KEY = 'vigil.view'
export const DEFAULT_VIEW: View = { tab: 'feed', kinds: [], persons: [] }

const VALID_TABS = new Set<string>(TABS.map((t) => t.key))
const SLUG = /^[a-zA-Z0-9_-]{1,64}$/

/** 逐字段校验：坏字段回落默认，好字段保留（一个坏字段不该毁掉整份记忆）。 */
export function readView(): View {
  let raw: string | null = null
  try {
    raw = localStorage.getItem(VIEW_STORAGE_KEY)
  } catch {
    return { ...DEFAULT_VIEW }
  }
  if (raw === null) return { ...DEFAULT_VIEW }
  let o: unknown
  try {
    o = JSON.parse(raw)
  } catch {
    return { ...DEFAULT_VIEW }
  }
  if (typeof o !== 'object' || o === null) return { ...DEFAULT_VIEW }
  const v = o as Record<string, unknown>
  const tab =
    typeof v.tab === 'string' && VALID_TABS.has(v.tab) ? (v.tab as Tab) : DEFAULT_VIEW.tab
  const kinds = Array.isArray(v.kinds)
    ? [...new Set(v.kinds.filter((k): k is string => typeof k === 'string' && SLUG.test(k)))]
    : []
  const persons = Array.isArray(v.persons)
    ? [
        ...new Set(
          v.persons.filter(
            (p): p is number => typeof p === 'number' && Number.isInteger(p) && p > 0,
          ),
        ),
      ]
    : []
  return { tab, kinds, persons }
}

export function writeView(v: View): void {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify(v))
  } catch {
    // 存不下只影响"下次还记得"，不该让当前这次交互失败
  }
}
